import asyncio
from playwright.async_api import async_playwright
from neo4j import GraphDatabase
from urllib.parse import urljoin
from collections import deque
import logging

# Neo4j 配置
NEO4J_URI = "bolt://localhost:7687"
NEO4J_USER = "neo4j"
NEO4J_PASSWORD = "26431043"

driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))

# 记录访问过的页面，防止死循环
visited_pages = set()
# 记录已保存元素的页面，避免重复保存
processed_pages = set()
# 最大深度
MAX_DEPTH = 2
# 待访问页面队列
page_queue = deque()

async def process_initial_page(page, current_url, depth):
    """处理初始页面加载和元素保存"""
    if depth > MAX_DEPTH:
        return

    print(f"Crawling: {current_url} (Depth: {depth})")
    visited_pages.add(current_url)

    # 确保DOM完全加载
    await page.wait_for_load_state('networkidle')

    title = await page.title()
    retry_count = 0
    while not title and retry_count < 3:
        await asyncio.sleep(3)
        title = await page.title()
        retry_count += 1
    if not title:
        logging.warning(f"Failed to get title for {current_url}")

    save_page_node(current_url, title)

    # 采集可点击元素并加入队列
    await collect_elements(page, current_url, depth)

async def collect_elements(page, current_url, depth):
    """采集页面中的可点击元素并加入队列"""
    elements = await page.query_selector_all('a[href], button, [onclick]')
    for elem in elements:
        try:
            # 检查元素是否可见且可点击
            if not await elem.is_visible() or not await elem.is_enabled():
                continue

            tag_name = await elem.evaluate("(node) => node.tagName.toLowerCase()")

            if tag_name == "a":
                href = await elem.get_attribute('href')
                if href:
                    new_url = urljoin(current_url, href)
                    if new_url not in visited_pages and new_url.startswith("http"):
                        page_queue.append((new_url, current_url, depth + 1))
            else:
                page_queue.append((current_url, current_url, depth + 1))
        except Exception as e:
            logging.error(f"Failed to add queue : {e}")

async def handle_navigation(page, current_url, depth, original_url):
    """处理页面跳转和新页面处理"""
    if depth > MAX_DEPTH:
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
                            await handle_page(new_page, new_url, depth + 1, original_url)

                            await new_page.close()
                        except Exception as e:
                            logging.error(f"Failed to crawl {new_url}: {e}")
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

                    # 确保元素可点击
                    await elem.scroll_into_view_if_needed()
                    await elem.hover()
                    await asyncio.sleep(0.5)

                    # 强制点击并等待
                    try:
                        await elem.click(force=True, timeout=5000)

                        # 增强页面跳转检测
                        try:
                            # 先等待可能的导航开始
                            await page.wait_for_event('request', timeout=3000)

                            # 精确等待URL变化
                            await page.wait_for_url(lambda url: url != old_url,
                                               timeout=10000,  # 增加超时时间
                                               wait_until='networkidle'
                                               )
                            new_url = page.url
                            print(f"[Page Redirected] {old_url} -> {new_url}")

                            # 确保新页面完全加载
                            await page.wait_for_load_state('networkidle')
                            await asyncio.sleep(1)

                            # 保存新页面信息
                            await handle_page(page, new_url, depth + 1, original_url)

                            # 确保返回原始页面
                            await page.go_back()
                            await page.wait_for_load_state('networkidle')
                            await asyncio.sleep(1)

                            # 验证是否成功返回
                            if page.url != old_url:
                                await page.reload()
                                await page.wait_for_load_state('networkidle')

                        except Exception as nav_error:
                            logging.error(f"Navigation error: {str(nav_error)}")
                            await page.reload()
                            await page.wait_for_load_state('networkidle')

                        # 检查是否有弹窗出现
                        try:
                            dialog = await page.wait_for_event('dialog', timeout=2000)
                            await dialog.dismiss()
                            print(f"Dismissed dialog on {page.url}")
                        except:
                            pass
                    except Exception as e:
                        logging.error(f"Click failed on {page.url}: {str(e)}")

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
                            await handle_page(new_page, new_url, depth + 1, original_url)
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
                            await handle_page(page, new_url, depth + 1, original_url)
                            return

                        # 添加连续URL变化检测
                        for _ in range(max_checks):
                            current_url = await page.evaluate('window.location.href')
                            if current_url != initial_url:  # 与初始URL比较
                                new_url = current_url
                                print(f"[URL Changed] {initial_url} -> {new_url}")
                                if new_url != old_url and new_url not in visited_pages and new_url.startswith("http"):
                                    save_jump_relation(old_url, new_url)
                                    await handle_page(page, new_url, depth + 1, original_url)
                                    return

                        print(f"[Click has no effect] on {old_url}")

                    # 确保处理完跳转页面后再继续
                    if new_url and new_url != old_url:
                        # 等待新页面完全加载
                        await page.wait_for_load_state('networkidle')
                        await asyncio.sleep(1)

                        # 保存新页面信息
                        await handle_page(page, new_url, depth + 1, original_url)

                        # 返回原始页面
                        if old_url != page.url:
                            await page.go_back()
                            await page.wait_for_load_state('networkidle')
                            await asyncio.sleep(2)  # 增加等待时间确保页面完全加载

                        # 重新获取页面元素，确保DOM已更新
                        elements = await page.query_selector_all('a[href], button, [onclick]')
                        for elem in elements:
                            tag_name = await elem.evaluate("(node) => node.tagName.toLowerCase()")
                            if tag_name == "button" and await elem.is_visible():
                                await elem.scroll_into_view_if_needed()
                                await elem.hover()
                                await asyncio.sleep(0.5)

                except Exception as e:
                    logging.error(f"[Handled Error] Click failed or invalid DOM: {e}")
        except Exception as e:
            logging.error(f"处理页面跳转和新页面处理 : {e}")

async def process_back(page, original_url, depth):
    """处理返回原始页面和剩余元素"""
    if depth > MAX_DEPTH:
        return

    # 返回原始页面
    await page.goto(original_url)
    await page.wait_for_load_state('networkidle')
    await asyncio.sleep(1)

    # 重新获取页面元素，确保DOM已更新
    elements = await page.query_selector_all('a[href], button, [onclick]')
    for elem in elements:
        try:
            tag_name = await elem.evaluate("(node) => node.tagName.toLowerCase()")

            if tag_name == "a":
                href = await elem.get_attribute('href')
                if href:
                    new_url = urljoin(original_url, href)
                    if new_url not in visited_pages and new_url.startswith("http"):
                        try:
                            new_page = await page.context.new_page()
                            await new_page.goto(new_url, timeout=20000)
                            await asyncio.sleep(5)

                            save_jump_relation(original_url, new_url)
                            await handle_page(new_page, new_url, depth + 1, original_url)

                            await new_page.close()
                        except Exception as e:
                            logging.error(f"Failed to crawl {new_url}: {e}")
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

                    # 确保元素可点击
                    await elem.scroll_into_view_if_needed()
                    await elem.hover()
                    await asyncio.sleep(0.5)

                    # 强制点击并等待
                    try:
                        await elem.click(force=True, timeout=5000)

                        # 增强页面跳转检测
                        try:
                            # 先等待可能的导航开始
                            await page.wait_for_event('request', timeout=3000)

                            # 精确等待URL变化
                            await page.wait_for_url(lambda url: url != old_url,
                                               timeout=10000,  # 增加超时时间
                                               wait_until='networkidle'
                                               )
                            new_url = page.url
                            print(f"[Page Redirected] {old_url} -> {new_url}")

                            # 确保新页面完全加载
                            await page.wait_for_load_state('networkidle')
                            await asyncio.sleep(1)

                            # 保存新页面信息
                            await handle_page(page, new_url, depth + 1, original_url)

                            # 确保返回原始页面
                            await page.go_back()
                            await page.wait_for_load_state('networkidle')
                            await asyncio.sleep(1)

                            # 验证是否成功返回
                            if page.url != old_url:
                                await page.reload()
                                await page.wait_for_load_state('networkidle')

                        except Exception as nav_error:
                            logging.error(f"Navigation error: {str(nav_error)}")
                            await page.reload()
                            await page.wait_for_load_state('networkidle')

                        # 检查是否有弹窗出现
                        try:
                            dialog = await page.wait_for_event('dialog', timeout=2000)
                            await dialog.dismiss()
                            print(f"Dismissed dialog on {page.url}")
                        except:
                            pass
                    except Exception as e:
                        logging.error(f"Click failed on {page.url}: {str(e)}")

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
                            await handle_page(new_page, new_url, depth + 1, original_url)
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
                            await handle_page(page, new_url, depth + 1, original_url)
                            return

                        # 添加连续URL变化检测
                        for _ in range(max_checks):
                            current_url = await page.evaluate('window.location.href')
                            if current_url != initial_url:  # 与初始URL比较
                                new_url = current_url
                                print(f"[URL Changed] {initial_url} -> {new_url}")
                                if new_url != old_url and new_url not in visited_pages and new_url.startswith("http"):
                                    save_jump_relation(old_url, new_url)
                                    await handle_page(page, new_url, depth + 1, original_url)
                                    return

                        print(f"[Click has no effect] on {old_url}")

                    # 确保处理完跳转页面后再继续
                    if new_url and new_url != old_url:
                        # 等待新页面完全加载
                        await page.wait_for_load_state('networkidle')
                        await asyncio.sleep(1)

                        # 保存新页面信息
                        await handle_page(page, new_url, depth + 1, original_url)

                        # 返回原始页面
                        if old_url != page.url:
                            await page.go_back()
                            await page.wait_for_load_state('networkidle')
                            await asyncio.sleep(2)  # 增加等待时间确保页面完全加载

                        # 重新获取页面元素，确保DOM已更新
                        elements = await page.query_selector_all('a[href], button, [onclick]')
                        for elem in elements:
                            tag_name = await elem.evaluate("(node) => node.tagName.toLowerCase()")
                            if tag_name == "button" and await elem.is_visible():
                                await elem.scroll_into_view_if_needed()
                                await elem.hover()
                                await asyncio.sleep(0.5)

                except Exception as e:
                    logging.error(f"[Handled Error] Click failed or invalid DOM: {e}")
        except Exception as e:
            logging.error(f"处理返回原始页面和剩余元素 : {e}")

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
                logging.error(f"Failed to save page node: {url}")
            else:
                print(f"Successfully saved page node: {record['p.url']}")
    except Exception as e:
        logging.error(f"Error saving page node {url}: {str(e)}")

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
                logging.error(f"Failed to save relation: {from_url} -> {to_url}")
            else:
                print(f"Successfully saved relation: {record['a.url']} -> {record['b.url']}")
    except Exception as e:
        logging.error(f"Error saving relation {from_url} -> {to_url}: {str(e)}")

async def save_elements(page, page_url):
    # 确保页面完全加载
    await page.wait_for_load_state('networkidle')
    await asyncio.sleep(2)  # 额外等待2秒确保动态内容加载完成

    # 检查页面是否已保存过元素
    if page_url in processed_pages:
        return

    processed_pages.add(page_url)
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
                    logging.error(f"Failed to save element {idx} on {page_url}")
                else:
                    print(f"Successfully saved element: {record['e.id']}")
        except Exception as e:
            logging.error(f"Failed to save element {idx} on {page_url}: {str(e)}")

async def handle_page(page, current_url, depth, original_url):
    if current_url in processed_pages:
        return

    await save_elements(page, current_url)
    await handle_navigation(page, current_url, depth, original_url)
    await process_back(page, original_url, depth)

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()

        start_url = "http://10.12.93.122:8090/#/blog/login"  # 起点页面
        await page.goto(start_url)

        # 处理初始页面
        await process_initial_page(page, start_url, depth=1)

        # 处理队列中的页面
        while page_queue:
            new_url, original_url, depth = page_queue.popleft()
            if new_url not in visited_pages:
                try:
                    new_page = await page.context.new_page()
                    await new_page.goto(new_url, timeout=20000)
                    await asyncio.sleep(5)

                    save_jump_relation(original_url, new_url)
                    await handle_page(new_page, new_url, depth, original_url)

                    await new_page.close()
                except Exception as e:
                    logging.error(f"Failed to crawl {new_url}: {e}")

        await browser.close()
        driver.close()

if __name__ == "__main__":
    asyncio.run(main())
