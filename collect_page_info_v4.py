import asyncio
from playwright.async_api import async_playwright
from neo4j import GraphDatabase
from urllib.parse import urljoin
import hashlib
import json

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

async def crawl(page, current_url, depth=0, original_page=None):
    if depth > MAX_DEPTH:
        return

    if current_url in visited_pages:
        return
        
    print(f"\n=== Crawling: {current_url} (Depth: {depth}) ===")
    visited_pages.add(current_url)

    try:
        # 确保页面加载完成
        await page.wait_for_load_state('networkidle', timeout=15000)
        # 额外等待SPA可能的数据加载
        await asyncio.sleep(2)
    except Exception as e:
        print(f"Timeout waiting for page load: {e}")
        return

    # 获取页面标题
    title = await page.title()
    print(f"Page title: {title}")
    
    # 保存页面节点
    save_page_node(current_url, title)
    
    # 保存当前页面元素（确保只保存一次）
    if current_url not in saved_pages:
        await save_elements(page, current_url)
        saved_pages.add(current_url)
    
    # 处理所有可点击元素
    elements = await page.query_selector_all('a[href], button, [onclick], [role="button"], input[type="button"], input[type="submit"]')
    print(f"Found {len(elements)} clickable elements")
    
    for idx, elem in enumerate(elements):
        try:
            tag_name = await elem.evaluate("(node) => node.tagName.toLowerCase()")
            print(f"\nProcessing element {idx+1}/{len(elements)} - {tag_name}")
            
            # 检查元素是否可见和可交互
            if not await elem.is_visible():
                print("Element not visible, skipping")
                continue
                
            if not await elem.is_enabled():
                print("Element not enabled, skipping")
                continue
                
            # 滚动到元素位置
            await elem.scroll_into_view_if_needed()
            await asyncio.sleep(0.5)
            
            # 记录点击前的状态
            old_url = page.url
            old_title = await page.title()
            
            if tag_name == "a":
                # 处理链接
                href = await elem.get_attribute('href')
                if not href:
                    print("No href attribute, skipping")
                    continue
                    
                new_url = urljoin(current_url, href)
                print(f"Link found: {new_url}")
                
                if new_url in visited_pages:
                    print("Already visited, skipping")
                    continue
                    
                if not new_url.startswith(("http", "javascript:")):
                    print("Not HTTP/JS URL, skipping")
                    continue
                
                # 特殊处理JavaScript链接
                if href.startswith("javascript:"):
                    print("Processing JavaScript link")
                    try:
                        await elem.click(timeout=5000)
                        await handle_spa_navigation(page, old_url, old_title, depth)
                        continue
                    except Exception as e:
                        print(f"Failed to process JS link: {e}")
                        continue
                
                # 在新标签页中打开链接
                try:
                    async with page.context.expect_page(timeout=5000) as new_page_info:
                        await elem.click(button="middle")  # 中键点击在新标签打开
                    new_page = await new_page_info.value
                    await new_page.wait_for_load_state('networkidle', timeout=15000)
                    
                    new_url = new_page.url
                    print(f"New page opened: {new_url}")
                    
                    # 保存跳转关系
                    save_jump_relation(current_url, new_url)
                    
                    # 递归爬取新页面
                    await crawl(new_page, new_url, depth + 1)
                    
                    await new_page.close()
                except Exception as e:
                    print(f"Failed to process link {new_url}: {e}")
                    # 回退方案：直接在当前页面点击
                    try:
                        await elem.click(timeout=5000, force=True)
                        await handle_spa_navigation(page, old_url, old_title, depth)
                    except Exception as e2:
                        print(f"Fallback click also failed: {e2}")
            else:
                # 处理按钮和其他可点击元素
                print("Processing clickable element")
                
                try:
                    # 尝试点击元素
                    await elem.click(timeout=5000, force=True)
                    await handle_spa_navigation(page, old_url, old_title, depth)
                    
                except Exception as e:
                    print(f"Failed to click element: {e}")
                    # 尝试强制点击
                    try:
                        await elem.click(force=True)
                        await handle_spa_navigation(page, old_url, old_title, depth)
                    except Exception as e2:
                        print(f"Force click also failed: {e2}")
                    
        except Exception as e:
            print(f"Error processing element {idx}: {e}")

async def handle_spa_navigation(page, old_url, old_title, depth):
    """专门处理SPA的路由变化"""
    max_checks = 30
    check_interval = 0.3
    
    # 1. 首先检查并处理登录表单（如果有）
    await handle_login_form(page)
    
    # 2. 然后处理页面导航变化
    for i in range(max_checks):
        try:
            current_url = await page.evaluate('window.location.href')
            current_title = await page.title()
            
            if current_url != old_url or current_title != old_title:
                print(f"SPA Navigation detected: {old_url} -> {current_url}")
                await asyncio.sleep(1)
                stable_url = await page.evaluate('window.location.href')
                if stable_url != current_url:
                    current_url = stable_url
                    print(f"URL stabilized to: {current_url}")
                
                save_jump_relation(old_url, current_url)
                
                try:
                    await page.wait_for_load_state('networkidle', timeout=10000)
                except:
                    pass
                    
                if current_url not in saved_pages:
                    await save_elements(page, current_url)
                    saved_pages.add(current_url)
                
                await crawl(page, current_url, depth + 1)
                
                if not await is_spa_page(page):
                    await page.go_back()
                    await page.wait_for_load_state('networkidle')
                return
            
            await asyncio.sleep(check_interval)
        except Exception as e:
            print(f"Navigation check error: {e}")
            await handle_dialog(page)

async def handle_login_form(page):
    """专门处理登录表单"""
    username_input = await page.query_selector('input.el-input__inner[type="text"]')
    password_input = await page.query_selector('input.el-input__inner[type="password"]')
    
    if not (username_input and password_input):
        return False
        
    print("检测到登录表单，开始处理...")
    
    try:
        # 0. 先关闭可能存在的错误弹窗
        await close_dialog_if_exists(page)
        await asyncio.sleep(0.5)
        
        # 1. 清除可能存在的旧值
        await username_input.fill('')
        await password_input.fill('')
        await asyncio.sleep(0.5)
        
        # 2. 模拟人工输入
        await username_input.focus()
        await page.keyboard.type('13702066833', delay=150)
        await asyncio.sleep(0.3)
        
        await password_input.focus()
        await page.keyboard.type('zhang26610741', delay=150)
        
        # 3. 验证输入是否正确
        filled_username = await username_input.input_value()
        filled_password = await password_input.input_value()
        
        if filled_username != '13702066833' or filled_password != 'zhang26610741':
            print("输入值不正确，重新填写")
            await username_input.fill('13702066833')
            await password_input.fill('zhang26610741')
        
        # 4. 检查即时错误
        if await check_for_errors(page):
            return False
        
        # 5. 提交表单
        submit_button = await page.query_selector('button.el-button--primary span:has-text("登录")')
        if submit_button:
            print("提交登录表单...")
            await submit_button.click()
            await asyncio.sleep(2)
            
            # 检查登录结果
            if await page.query_selector('.el-message--error'):
                error_text = await page.inner_text('.el-message--error')
                print(f"登录失败: {error_text}")
                return False
            return True
        return False
    except Exception as e:
        print(f"登录处理异常: {e}")
        return False

async def close_dialog_if_exists(page):
    """关闭可能存在的弹窗"""
    try:
        dialog = await page.query_selector('.el-dialog__wrapper')
        if dialog and await dialog.is_visible():
            print("检测到弹窗，尝试关闭")
            close_btn = await dialog.query_selector('.el-dialog__close, button.el-button')
            if close_btn:
                await close_btn.click()
                await asyncio.sleep(1)
                return True
    except Exception as e:
        print(f"关闭弹窗失败: {e}")
    return False

async def check_for_errors(page):
    """检查表单验证错误"""
    try:
        error_msg = await page.query_selector('.el-form-item__error, .el-message--error')
        if error_msg and await error_msg.is_visible():
            error_text = await error_msg.inner_text()
            print(f"表单验证错误: {error_text}")
            return True
    except Exception as e:
        print(f"检查错误时出错: {e}")
    return False

async def handle_dialog(page):
    """处理各种弹窗"""
    try:
        alert = await page.query_selector('body div[role="dialog"] button[type="button"] span')
        if alert and await alert.is_visible():
            print("Alert detected, closing...")
            await alert.click()
            await asyncio.sleep(1)
    except Exception as e:
        print(f"处理弹窗失败: {e}")
# async def handle_spa_navigation(page, old_url, old_title, depth):
#     """专门处理SPA的路由变化"""
#     max_checks = 30
#     check_interval = 0.3
    
#     # 处理登录表单（如果存在）
#     username_input = await page.query_selector('input.el-input__inner[type="text"]')
#     password_input = await page.query_selector('input.el-input__inner[type="password"]')
    
#     if username_input and password_input:
#         print("Login form detected, filling in test credentials...")
#         try:
#             await username_input.fill('13702066833')
#             await password_input.fill('zhang26610741')
#             await asyncio.sleep(1)
            
#             # 检查是否有即时错误
#             error_msg = await page.query_selector('body div[role="dialog"] button[type="button"] span')
#             if error_msg:
#                 error_text = await error_msg.inner_text()
#                 print(f"即时验证错误: {error_text}")
#                 return
            
#             await asyncio.sleep(1)
            
#             # 尝试提交表单
#             submit_button = await page.query_selector('button.el-button--primary span:has-text("登录")')
#             if submit_button:
#                 await submit_button.click()
#                 await asyncio.sleep(2)  # 等待表单提交
#         except Exception as e:
#             print(f"Failed to fill login form: {e}")
    
#     for i in range(max_checks):
#         try:
#             current_url = await page.evaluate('window.location.href')
#             current_title = await page.title()
            
#             # 检查URL或标题是否变化
#             if current_url != old_url or current_title != old_title:
#                 print(f"SPA Navigation detected: {old_url} -> {current_url}")
                
#                 # 确保页面稳定
#                 await asyncio.sleep(1)
#                 stable_url = await page.evaluate('window.location.href')
#                 if stable_url != current_url:
#                     current_url = stable_url
#                     print(f"URL stabilized to: {current_url}")
                
#                 # 保存跳转关系
#                 save_jump_relation(old_url, current_url)
                
#                 # 确保页面加载完成
#                 try:
#                     await page.wait_for_load_state('networkidle', timeout=10000)
#                 except:
#                     pass
                    
#                 # 保存新页面元素
#                 if current_url not in saved_pages:
#                     await save_elements(page, current_url)
#                     saved_pages.add(current_url)
                
#                 # 递归处理新页面
#                 await crawl(page, current_url, depth + 1)
                
#                 # 返回原页面（如果是SPA则不需要）
#                 if not await is_spa_page(page):
#                     await page.go_back()
#                     await page.wait_for_load_state('networkidle')
#                 return
            
#             await asyncio.sleep(check_interval)
#         except Exception as e:
#             print(f"Navigation check error: {e}")
#             # 处理弹窗（如果有）
#             try:
#                 alert = await page.query_selector('body div[role="dialog"] button[type="button"] span')
#                 if alert:
#                     print("Alert detected, closing...")
#                     await alert.click()
#                     await asyncio.sleep(1)
#                     # 重新记录当前URL和标题
#                     old_url = await page.evaluate('window.location.href')
#                     old_title = await page.title()
#             except Exception as e2:
#                 print(f"Failed to close alert: {e2}")
#                 continue
    
#     print("No SPA navigation detected after click")

async def is_spa_page(page):
    """检测是否是单页应用"""
    try:
        return await page.evaluate("""() => {
            return window.history && window.history.pushState && 
                   (window.__SPA || window.angular || window.__NUXT__ || 
                    window.__NEXT_DATA__ || window.__REDUX_STATE__);
        }""")
    except:
        return False

def save_page_node(url, title):
    try:
        with driver.session() as session:
            result = session.run(
                """
                MERGE (p:Page {url: $url}) 
                SET p.title = $title,
                    p.visited_at = datetime()
                RETURN p.url
                """,
                url=url,
                title=title if title else "No title"
            )
            
            record = result.single()
            if record:
                print(f"Saved page node: {record['p.url']}")
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
                SET r.from_url = $from_url, 
                    r.to_url = $to_url,
                    r.timestamp = datetime()
                RETURN a.url, b.url
                """,
                from_url=from_url,
                to_url=to_url
            )
            
            record = result.single()
            if record:
                print(f"Saved relation: {record['a.url']} -> {record['b.url']}")
    except Exception as e:
        print(f"Error saving relation {from_url} -> {to_url}: {str(e)}")

async def save_elements(page, page_url):
    print(f"\nSaving elements for: {page_url}")
    # 扩展选择器以捕获更多交互元素
    elements = await page.query_selector_all('''
        button, input, a, select, textarea, 
        [role="button"], [role="link"], 
        [onclick], [onclick*="location.href"], 
        [data-testid], [data-qa], [data-test], [data-test-id],
        [data-cy], [data-testing], [data-tid]
    ''')
    print(f"Found {len(elements)} elements to save")
    
    for idx, elem in enumerate(elements):
        try:
            # 获取元素基本信息
            tag = await elem.evaluate("(node) => node.tagName.toLowerCase()")
            text = (await elem.inner_text() or "").strip()
            placeholder = await elem.get_attribute("placeholder") or ""
            title = await elem.get_attribute("title") or ""
            value = await elem.get_attribute("value") or ""
            name = await elem.get_attribute("name") or ""
            id_attr = await elem.get_attribute("id") or ""
            class_list = await elem.get_attribute("class") or ""
            
            # 获取XPath
            xpath = await page.evaluate(
                """(el) => {
                    function getXPath(el) {
                        if (!el) return '';
                        if (el.id !== '') return 'id(\"' + el.id + '\")';
                        if (el === document.body) return el.tagName;
                        if (!el.parentNode) return el.tagName;
                        let ix = 0;
                        const siblings = el.parentNode.childNodes;
                        for (let i = 0; i < siblings.length; i++) {
                            const sibling = siblings[i];
                            if (sibling === el) return getXPath(el.parentNode) + '/' + el.tagName + '[' + (ix + 1) + ']';
                            if (sibling.nodeType === 1 && sibling.tagName === el.tagName) ix++;
                        }
                        return el.tagName;
                    }
                    return getXPath(el);
                }""",
                elem
            )
            
            # 获取CSS选择器
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
                                if (sib.nodeName.toLowerCase() == selector)
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

            # 获取邻居文本
            neighbor_texts = await elem.evaluate("""
                e => {
                    let texts = [];
                    let current = e;
                    // 获取前5个兄弟元素的文本
                    for (let i = 0; i < 5; i++) {
                        current = current.previousElementSibling;
                        if (!current) break;
                        if (current.innerText && current.innerText.trim()) {
                            texts.push(current.innerText.trim());
                        }
                    }
                    // 获取父元素的文本
                    if (e.parentNode && e.parentNode.innerText) {
                        texts.push(e.parentNode.innerText.trim());
                    }
                    return texts.filter(t => t.length > 0);
                }
            """) or []
            
            # 获取所有自定义数据属性
            data_attributes = await elem.evaluate("""
                e => {
                    const attrs = {};
                    for (let attr of e.attributes) {
                        if (attr.name.startsWith('data-')) {
                            attrs[attr.name] = attr.value;
                        }
                    }
                    return attrs;
                }
            """) or {}
            
            # 获取元素在视口中的位置（转换为字符串存储）
            bounding_box = await elem.bounding_box()
            position = "Not available"
            if bounding_box:
                position = f"x:{bounding_box['x']}, y:{bounding_box['y']}, width:{bounding_box['width']}, height:{bounding_box['height']}"
            
            # 获取元素样式（转换为字符串存储）
            computed_style = await elem.evaluate("""
                e => {
                    const style = {};
                    const computed = window.getComputedStyle(e);
                    for (let i = 0; i < computed.length; i++) {
                        const prop = computed[i];
                        style[prop] = computed.getPropertyValue(prop);
                    }
                    return JSON.stringify(style);
                }
            """) or "{}"
            
            # 获取元素属性（转换为字符串存储）
            element_info = await elem.evaluate("""
                (el) => {
                    const attrs = {};
                    for (let attr of el.attributes) {
                        attrs[attr.name] = attr.value;
                    }
                    return JSON.stringify({
                        id: el.id || null,
                        name: el.name || null,
                        placeholder: el.placeholder || null,
                        type: el.type || null,
                        value: el.value || null,
                        checked: el.checked || null,
                        disabled: el.disabled || null,
                        readOnly: el.readOnly || null,
                        required: el.required || null,
                        tabIndex: el.tabIndex || null,
                        aria_label: el.getAttribute('aria-label') || null,
                        aria_role: el.getAttribute('role') || null,
                        title: el.title || null,
                        className: el.className || null,
                        attributes: attrs,
                        data_attributes: Object.fromEntries(
                            Array.from(el.attributes)
                                .filter(attr => attr.name.startsWith('data-'))
                                .map(attr => [attr.name, attr.value])
                        )
                    });
                }
            """) or "{}"
            
            # 计算元素优先级
            priority = 3  # 默认优先级
            element_info_dict = json.loads(element_info)
            if element_info_dict.get('id'):
                priority = 0
            elif element_info_dict.get('data-testid') or element_info_dict.get('data-qa'):
                priority = 1
            elif element_info_dict.get('name'):
                priority = 2
            
            # 生成唯一control_id
            control_id = hashlib.md5((
                tag + text + xpath + css_selector + 
                str(element_info_dict.get('id')) + 
                str(element_info_dict.get('data-testid'))
            ).encode()).hexdigest()
            
            # 推断元素类型
            element_type = "unknown"
            if tag in ["button", "a"] or (element_info_dict.get("type") in ["button", "submit"]):
                element_type = "button"
            elif tag in ["input", "textarea"] or (element_info_dict.get("type") in ["text", "password", "email", "search"]):
                element_type = "input"
            elif tag == "img":
                element_type = "image"
            elif tag == "select":
                element_type = "dropdown"
            elif "aria_role" in element_info_dict:
                element_type = element_info_dict["aria_role"]  # 使用ARIA角色
            
            # 保存到Neo4j
            with driver.session() as session:
                result = session.run(
                    """
                    MATCH (p:Page {url: $page_url})
                    MERGE (e:Element {control_id: $control_id})
                    SET e.tag = $tag,
                        e.text = $text,
                        e.placeholder = $placeholder,
                        e.title = $title,
                        e.value = $value,
                        e.name = $name,
                        e.id_attr = $id_attr,
                        e.classes = $class_list,
                        e.xpath = $xpath,
                        e.css_selector = $css_selector,
                        e.neighbor_texts = $neighbor_texts,
                        e.position = $position,
                        e.computed_style = $computed_style,
                        e.element_type = $element_type,
                        e.attrs = $attrs,
                        e.priority = $priority,
                        e.last_updated = datetime()
                    MERGE (p)-[:CONTAINS]->(e)
                    RETURN e.control_id
                    """,
                    page_url=page_url,
                    control_id=control_id,
                    tag=tag,
                    text=text,
                    placeholder=placeholder,
                    title=title,
                    value=value,
                    name=name,
                    id_attr=id_attr,
                    class_list=class_list,
                    xpath=xpath,
                    css_selector=css_selector,
                    neighbor_texts=neighbor_texts,
                    position=position,
                    computed_style=computed_style,
                    element_type=element_type,
                    attrs=element_info,
                    priority=priority
                )
                
                record = result.single()
                if record:
                    print(f"Saved element {idx+1}: {tag} ({element_type}) - {control_id}")
                    
        except Exception as e:
            print(f"Failed to save element {idx}: {str(e)}")

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(
            # viewport={'width': 1920, 'height': 1080},
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        )
        page = await context.new_page()

        start_url = "http://10.12.93.122:8090/#/blog/login"  # 起点页面
        try:
            await page.goto(start_url, timeout=60000)
            await page.wait_for_load_state('networkidle', timeout=20000)
            await crawl(page, start_url)
        except Exception as e:
            print(f"Initial page load failed: {e}")
        finally:
            await browser.close()
            driver.close()

if __name__ == "__main__":
    asyncio.run(main())