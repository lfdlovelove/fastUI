import asyncio
from playwright.async_api import async_playwright
from neo4j import GraphDatabase
from urllib.parse import urljoin

# Neo4j 配置
NEO4J_URI = "bolt://localhost:7687"
NEO4J_USER = "neo4j"
NEO4J_PASSWORD = "26431043"  

driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))

# 记录访问过的页面，防止死循环
visited_pages = set()
# 记录已保存元素的页面，避免重复保存
saved_pages = set()

# 最大深度
MAX_DEPTH = 2

async def crawl(page, current_url, depth, url_list=None, original_page=None):
    if depth > MAX_DEPTH:
        return

    print(f"Crawling: {current_url} (Depth: {depth})")
    visited_pages.add(current_url)

    await page.wait_for_load_state('networkidle')

    title = await page.title()
    retry_count = 0
    while not title and retry_count < 3:
        await asyncio.sleep(3)
        title = await page.title()
        retry_count += 1
    if not title:
        print(f"Warning: Failed to get title for {current_url}")

    save_page_node(current_url, title)
    
    # 保存当前页面的所有元素（确保只保存一次）
    if current_url not in saved_pages:
        await save_elements(page, current_url)
        saved_pages.add(current_url)
    
    # 记录原始页面（首次访问时）
    if original_page is None:
        original_page = page
        
    # 如果是跳转后的页面，处理完后返回原始页面
    if depth > 0:
        await page.close()
        # 返回原始页面继续处理其他元素
        if original_page and not original_page.is_closed():
            await original_page.bring_to_front()
            await original_page.wait_for_load_state('networkidle')
            await process_remaining_elements(original_page, current_url)
            
            # 重新获取页面元素，确保不会重复处理
            elements = await original_page.query_selector_all('button, a[href]')
            for elem in elements:
                try:
                    if not await elem.is_visible():
                        continue
                        
                    # 检查元素是否已处理
                    tag = await elem.evaluate("(node) => node.tagName.toLowerCase()")
                    if tag == "a":
                        href = await elem.get_attribute('href')
                        if href and not href.startswith("http"):
                            continue
                            
                    # 模拟点击
                    await elem.click()
                    await asyncio.sleep(1)
                    
                    # 检查是否跳转
                    new_url = original_page.url
                    if new_url != current_url:
                        # 处理新页面
                        await save_elements(original_page, new_url)
                        save_jump_relation(current_url, new_url)
                        
                        # 返回原始页面
                        await original_page.go_back()
                        await original_page.wait_for_load_state('networkidle')
                        
                except Exception as e:
                    print(f"Error processing element: {str(e)}")
        return

    # 如果传入url_list，只处理这些
    if url_list:
        for new_url in url_list:
            if new_url not in visited_pages and new_url.startswith("http"):
                try:
                    new_page = await page.context.new_page()
                    await new_page.goto(new_url, timeout=20000)
                    await asyncio.sleep(5)

                    save_jump_relation(current_url, new_url)
                    await save_elements(new_page, new_url)

                    await crawl(new_page, new_url, depth + 1)

                    await new_page.close()
                except Exception as e:
                    print(f"Failed to crawl {new_url}: {e}")
        return

    # 页面上所有a和button元素
    elements = await page.query_selector_all('a[href], button, [onclick]')
    for elem in elements:
        try:
            tag_name = await elem.evaluate("(node) => node.tagName.toLowerCase()")

            if tag_name == "a":
                href = await elem.get_attribute('href')
                if href:
                    new_url = urljoin(current_url, href)
                    if new_url not in visited_pages and new_url.startswith("http"):
                        try:
                            new_page = await page.context.new_page()
                            await new_page.goto(new_url, timeout=20000)
                            await asyncio.sleep(5)

                            save_jump_relation(current_url, new_url)
                            await save_elements(new_page, new_url)

                            await crawl(new_page, new_url, depth + 1)

                            await new_page.close()
                        except Exception as e:
                            print(f"Failed to crawl {new_url}: {e}")
            else:
                try:
                    old_url = page.url

                    # 元素仍在DOM中且可见
                    if not await elem.is_visible():
                        continue

                    await elem.scroll_into_view_if_needed()
                    await asyncio.sleep(0.2)
                    
                    # 检查是否是登录表单
                    login_form = await page.query_selector('form[action*="login"], form:has(input[type="password"])')
                    if login_form:
                        # 填写测试账号
                        await page.fill('input[type="text"]', '13702066833')
                        await page.fill('input[type="password"]', 'zhang26610741')
                        await asyncio.sleep(0.5)

                    new_page = None
                    new_url = None

                    # 监听是否有新tab打开
                    try:
                        async with page.context.expect_page(timeout=3000) as page_info:
                            await elem.click(force=True)
                        new_page = await page_info.value
                        await new_page.wait_for_load_state("load")
                        new_url = new_page.url
                        print(f"[New Tab Opened] {old_url} -> {new_url}")

                        if new_url not in visited_pages and new_url.startswith("http"):
                            save_jump_relation(old_url, new_url)
                            await save_elements(new_page, new_url)
                            await crawl(new_page, new_url, depth + 1)
                        await new_page.close()

                    except asyncio.TimeoutError:
                        # 没有新tab，可能是当前页跳转或无跳转
                        await elem.click(force=True)
                        await page.wait_for_load_state("load")
                        # 增强SPA路由变化检测
                        max_checks = 50  # 大幅增加检测次数
                        check_interval = 0.1  # 最小化检测间隔
                        new_url = old_url
                        
                        # 立即捕获点击后的初始URL
                        initial_url = page.url
                        
                        # 添加即时URL变化检测
                        immediate_check = await page.evaluate('window.location.href')
                        if immediate_check != initial_url:
                            new_url = immediate_check
                            print(f"[Immediate URL Change] {initial_url} -> {new_url}")
                            save_jump_relation(old_url, new_url)
                            await save_elements(page, new_url)
                            await crawl(page, new_url, depth + 1)
                            return
                        
                        # 添加连续URL变化检测
                        for _ in range(max_checks):
                            current_url = await page.evaluate('window.location.href')
                            if current_url != initial_url:  # 与初始URL比较
                                new_url = current_url
                                print(f"[URL Changed] {initial_url} -> {new_url}")
                                if new_url != old_url and new_url not in visited_pages and new_url.startswith("http"):
                                    save_jump_relation(old_url, new_url)
                                    await save_elements(page, new_url)
                                    await crawl(page, new_url, depth + 1)
                                    return
                            await asyncio.sleep(check_interval)
                        
                        print(f"[Click has no effect] on {old_url}")

                except Exception as e:
                    print(f"[Handled Error] Click failed or invalid DOM: {e}")

        except Exception as e:
            print(f"system error: {e}")



def save_page_node(url, title):
    try:
        with driver.session() as session:
            result = session.run(
                """
                MERGE (p:Page {url: $url}) 
                SET p.title = $title
                RETURN p.url
                """,
                url=url,
                title=title
            )
            
            # 验证节点是否保存成功
            record = result.single()
            if not record:
                print(f"Failed to save page node: {url}")
            else:
                print(f"Successfully saved page node: {record['p.url']}")
    except Exception as e:
        print(f"Error saving page node {url}: {str(e)}")

def save_jump_relation(from_url, to_url):
    try:
        with driver.session() as session:
            print(f"Saving relation: {from_url} -> {to_url}")
            
            # 确保两个页面节点都存在
            session.run("MERGE (a:Page {url: $from_url})", from_url=from_url)
            session.run("MERGE (b:Page {url: $to_url})", to_url=to_url)
            
            # 创建关系
            result = session.run(
                """
                MATCH (a:Page {url: $from_url}), (b:Page {url: $to_url})
                MERGE (a)-[r:JUMP_TO]->(b)
                SET r.from_url = $from_url, r.to_url = $to_url
                RETURN a.url, b.url
                """,
                from_url=from_url,
                to_url=to_url
            )
            
            # 验证关系是否保存成功
            record = result.single()
            if not record:
                print(f"Failed to save relation: {from_url} -> {to_url}")
            else:
                print(f"Successfully saved relation: {record['a.url']} -> {record['b.url']}")
    except Exception as e:
        print(f"Error saving relation {from_url} -> {to_url}: {str(e)}")

async def process_remaining_elements(page, current_url):
    """处理原始页面剩余未处理的元素"""
    # 获取所有按钮和链接元素
    elements = await page.query_selector_all('button, a[href]')
    for elem in elements:
        try:
            # 检查元素是否已处理
            if not await elem.is_visible():
                continue
                
            # 处理点击逻辑
            tag = await elem.evaluate("(node) => node.tagName.toLowerCase()")
            if tag == "a":
                href = await elem.get_attribute('href')
                if href and not href.startswith("http"):
                    continue
                
            # 模拟点击
            await elem.click()
            await asyncio.sleep(1)
            
            # 检查是否跳转
            new_url = page.url
            if new_url != current_url:
                # 处理新页面
                await save_elements(page, new_url)
                save_jump_relation(current_url, new_url)
                
                # 返回原始页面
                await page.go_back()
                await page.wait_for_load_state('networkidle')
                
        except Exception as e:
            print(f"Error processing element: {str(e)}")

async def save_elements(page, page_url):
    elements = await page.query_selector_all('button, input, a, select, textarea')
    for idx, elem in enumerate(elements):
        try:
            # 检查是否为HTMLElement
            is_html_element = await elem.evaluate("(node) => node instanceof HTMLElement")
            if not is_html_element:
                continue
                
            tag = await elem.evaluate("(node) => node.tagName.toLowerCase()")
            text = await elem.inner_text() or ""
            placeholder = await elem.get_attribute("placeholder") or ""
            title = await elem.get_attribute("title") or ""
            xpath = await page.evaluate(
                """(el) => {
                    function getXPath(el) {
                        if (!el) return '';
                        if (el.id !== '') return 'id(\"' + el.id + '\")';
                        if (el === document.body) return el.tagName;
                        if (!el.parentNode) return el.tagName;
                        let ix = 0;
                        const siblings = el.parentNode ? el.parentNode.childNodes : [];
                        for (let i = 0; i < siblings.length; i++) {
                            const sibling = siblings[i];
                            if (!sibling) continue;
                            if (sibling === el) return getXPath(el.parentNode) + '/' + el.tagName + '[' + (ix + 1) + ']';
                            if (sibling.nodeType === 1 && sibling.tagName === el.tagName) ix++;
                        }
                        return el.tagName;
                    }
                    return getXPath(el);
                }""",
                elem
            )
            
            # 获取邻居文本
            neighbor_texts = []
            try:
                neighbors = await elem.evaluate_handle("""
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
                neighbor_texts = await neighbors.json_value()
            except:
                pass
                
            # # 生成control_id
            # import hashlib
            # control_id = hashlib.md5((tag + text + xpath).encode()).hexdigest()

            # 新增提取 css selector
            css_selector = await page.evaluate(
                """(el) => {
                    if (!el) return '';
                    let path = [];
                    while (el && el.nodeType === Node.ELEMENT_NODE) {
                        let selector = el.nodeName.toLowerCase();
                        if (el.id) {
                            selector += '#' + el.id;
                            path.unshift(selector);
                            break;
                        } else {
                            let sib = el, nth = 1;
                            while (sib = sib.previousElementSibling) {
                                if (sib && sib.nodeName.toLowerCase() == selector)
                                    nth++;
                            }
                            if (nth != 1)
                                selector += ":nth-of-type(" + nth + ")";
                        }
                        path.unshift(selector);
                        el = el.parentNode;
                    }
                    return path.join(" > ");
                }""",
                elem
            )

            # 进一步提取有意义的属性
            element_info = await elem.evaluate(
                """(el) => {
                    return {
                        id: el.id || null,
                        name: el.name || null,
                        placeholder: el.placeholder || null,
                        'data-testid': el.getAttribute('data-testid') || null,
                        type: el.type || null,
                        aria_label: el.getAttribute('aria-label') || null
                    }
                }"""
            )
            
            # 计算元素优先级
            priority = 2
            if element_info.get('id'):
                priority = 0
            elif element_info.get('data-testid'):
                priority = 1
            
            # 推断元素类型
            element_type = "unknown"
            if tag in ["BUTTON", "A"] or (element_info.get("type") in ["button", "submit"]):
                element_type = "button"
            elif tag in ["INPUT", "TEXTAREA"] or (element_info.get("type") in ["text", "password", "email", "search"]):
                element_type = "input"
            elif tag == "IMG":
                element_type = "image"

            with driver.session() as session:
                result = session.run(
                    """
                    MATCH (p:Page {url: $page_url})
                    CREATE (e:Element {
                        id: $id,
                        tag: $tag,
                        text: $text,
                        placeholder: $placeholder,
                        title: $title,
                        xpath: $xpath,
                        neighbor_texts: $neighbor_texts,
                        css_selector: $css_selector,
                        element_type: $element_type,
                        attrs: $attrs,
                        priority: $priority
                    })
                    CREATE (p)-[:CONTAINS]->(e)
                    RETURN e.id
                    """,
                    page_url=page_url,
                    id=f"{page_url}#{idx}",
                    tag=tag,
                    text=text,
                    placeholder=placeholder,
                    title=title,
                    xpath=xpath,
                    neighbor_texts=neighbor_texts,
                    # control_id=control_id,
                    css_selector=css_selector,
                    element_type=element_type,
                    attrs=str(element_info),
                    priority=priority
                )
                
                # 验证元素是否保存成功
                record = result.single()
                if not record:
                    print(f"Failed to save element {idx} on {page_url}")
                else:
                    print(f"Successfully saved element: {record['e.id']}")
        except Exception as e:
            print(f"Failed to save element {idx} on {page_url}: {str(e)}")

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()

        start_url = "http://10.12.93.122:8090/#/blog/login"  # 起点页面
        await page.goto(start_url)
        
        # 单页面模式
        await crawl(page, start_url, depth=2)

        # 列表模式
        # urls = ["https://example.com/page1", "https://example.com/page2"]
        # await crawl(page, start_url, depth=1, url_list=urls)

        await browser.close()
        driver.close()

if __name__ == "__main__":
    asyncio.run(main())
