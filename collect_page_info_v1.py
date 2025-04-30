# 安装依赖
# pip install playwright neo4j

from playwright.sync_api import sync_playwright
from neo4j import GraphDatabase
import hashlib

# 连接Neo4j
driver = GraphDatabase.driver("bolt://localhost:7687", auth=("neo4j", "26431043"))

# 采集控件信息并存入Neo4j
def collect_controls(url):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page = browser.new_page()
        inject_xpath_script(page)
        page.goto(url)

        # 抓取所有可交互控件，比如按钮、输入框
        elements = page.query_selector_all('button, input, a, select, textarea')

        for el in elements:
            try:
                tag = el.evaluate("e => e.tagName.toLowerCase()")
                text = el.inner_text() or ""
                placeholder = el.get_attribute("placeholder") or ""
                title = el.get_attribute("title") or ""
                xpath = el.evaluate("e => generateXPath(e)")

                # 邻居元素（上方5个文本兄弟）
                neighbor_texts = []
                try:
                    neighbors = el.evaluate_handle("""
                        e => {
                            let texts = [];
                            let current = e;
                            while (current = current.previousElementSibling) {
                                if (current.innerText) {
                                    texts.push(current.innerText.trim());
                                }
                                if (texts.length >= 5) break;
                            }
                            return texts;
                        }
                    """)
                    neighbor_texts = neighbors.json_value()
                except:
                    pass

                # 控件唯一ID
                control_id = hashlib.md5((tag + text + xpath).encode()).hexdigest()

                # 插入Neo4j
                with driver.session() as session:
                    session.run(
                        """
                        MERGE (c:Control {control_id: $control_id})
                        SET c.tag = $tag,
                            c.text = $text,
                            c.placeholder = $placeholder,
                            c.title = $title,
                            c.xpath = $xpath,
                            c.page_url = $page_url,
                            c.neighbor_texts = $neighbor_texts
                        """,
                        control_id=control_id,
                        tag=tag,
                        text=text,
                        placeholder=placeholder,
                        title=title,
                        xpath=xpath,
                        page_url=url,
                        neighbor_texts=neighbor_texts
                    )

            except Exception as e:
                print(f"Error processing element: {e}")

        browser.close()

# 补充一个小JS函数生成XPath（Playwright里直接注入）
xpath_script = """
window.generateXPath = function (element) {
    if (element.id !== '') {
        return 'id("' + element.id + '")';
    }
    if (element === document.body) {
        return '/html/body';
    }
    var ix = 0;
    var siblings = element.parentNode.childNodes;
    for (var i=0; i<siblings.length; i++) {
        var sibling = siblings[i];
        if (sibling === element) {
            return generateXPath(element.parentNode) + '/' + element.tagName.toLowerCase() + '[' + (ix+1) + ']';
        }
        if (sibling.nodeType === 1 && sibling.tagName === element.tagName) {
            ix++;
        }
    }
};
"""

# 页面加载完注入
def inject_xpath_script(page):
    page.add_init_script(xpath_script)

# 主程序
if __name__ == "__main__":
    url = "https://www.baidu.com/"
    collect_controls(url)
    print("采集完成✅")
