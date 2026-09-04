"""API 测试脚本 - 演示如何使用观察列表和扫描功能"""

import requests
import json
import time

BASE_URL = "http://localhost:8080"


def test_health():
    """测试健康检查"""
    print("🏥 测试健康检查...")
    resp = requests.get(f"{BASE_URL}/health")
    print(f"状态码: {resp.status_code}")
    print(f"响应: {json.dumps(resp.json(), indent=2, ensure_ascii=False)}\n")


def test_add_watch_items():
    """添加监控项"""
    print("👀 添加要监控的货...\n")

    items = [
        {
            "sku": "JR5408",
            "size": "US 10",
            "name": "Adidas Ultraboost 22",
            "color": "White/Black",
        },
        {
            "sku": "JR5410",
            "size": "US 11",
            "name": "Adidas Yeezy Boost 350",
            "color": "Core Black",
        },
        {
            "sku": "JR5412",
            "size": "US 9.5",
            "name": "Adidas NMD R1",
            "color": "Refined Blue",
        },
    ]

    watch_ids = []
    for item in items:
        print(f"  添加: {item['sku']} {item['size']}")
        resp = requests.post(f"{BASE_URL}/watch", json=item)
        if resp.status_code == 200:
            data = resp.json()
            watch_ids.append(data["id"])
            print(f"    ✅ 成功 (ID: {data['id'][:8]}...)\n")
        else:
            print(f"    ❌ 失败: {resp.text}\n")

    return watch_ids


def test_list_watch_items():
    """列出所有监控项"""
    print("📋 列出所有监控项...\n")
    resp = requests.get(f"{BASE_URL}/watch")

    if resp.status_code == 200:
        items = resp.json()
        print(f"总共 {len(items)} 个监控项:")
        for item in items:
            print(
                f"  • {item['sku']} - {item['size']} ({item['name']} / {item['color']})"
            )
        print()
    else:
        print(f"❌ 失败: {resp.text}\n")


def test_disable_watch_item(item_id):
    """禁用监控项"""
    print(f"🔇 禁用监控项 {item_id[:8]}...\n")
    resp = requests.patch(f"{BASE_URL}/watch/{item_id}/disable")

    if resp.status_code == 200:
        print(f"  ✅ 已禁用\n")
    else:
        print(f"  ❌ 失败: {resp.text}\n")


def test_enable_watch_item(item_id):
    """启用监控项"""
    print(f"🔊 启用监控项 {item_id[:8]}...\n")
    resp = requests.patch(f"{BASE_URL}/watch/{item_id}/enable")

    if resp.status_code == 200:
        print(f"  ✅ 已启用\n")
    else:
        print(f"  ❌ 失败: {resp.text}\n")


def test_get_notifications():
    """获取通知历史"""
    print("📬 获取通知历史...\n")
    resp = requests.get(f"{BASE_URL}/notifications?limit=10")

    if resp.status_code == 200:
        notifications = resp.json()
        if notifications:
            print(f"最近 {len(notifications)} 条通知:")
            for notif in notifications:
                print(
                    f"  • [{notif['sent_at']}] {notif['sku']} - {notif['size']}: {notif['message']}"
                )
        else:
            print("  （暂无通知）")
        print()
    else:
        print(f"❌ 失败: {resp.text}\n")


def test_delete_watch_item(item_id):
    """删除监控项"""
    print(f"🗑️  删除监控项 {item_id[:8]}...\n")
    resp = requests.delete(f"{BASE_URL}/watch/{item_id}")

    if resp.status_code == 200:
        print(f"  ✅ 已删除\n")
    else:
        print(f"  ❌ 失败: {resp.text}\n")


def test_trigger_scan():
    """手动触发扫描"""
    print("🚀 手动触发扫描...\n")

    # 只扫描两个 SKU 做演示
    payload = {"skus": ["JR5408", "JR5410"]}

    resp = requests.post(f"{BASE_URL}/scan", json=payload)

    if resp.status_code == 200:
        print(f"  ✅ 扫描已排队")
        print(f"  响应: {json.dumps(resp.json(), indent=2, ensure_ascii=False)}\n")
        print("  注意：实际扫描在后台运行，请等待几秒后查看通知历史\n")
    else:
        print(f"  ❌ 失败: {resp.text}\n")


def test_slack_config():
    """检查 Slack 配置"""
    print("🔗 检查 Slack 配置...\n")
    resp = requests.get(f"{BASE_URL}/config/slack")

    if resp.status_code == 200:
        config = resp.json()
        print(f"  Slack 启用: {config.get('enabled', False)}")
        print(f"  Webhook 已配置: {config.get('webhook_configured', False)}")
        print(f"  用户 mention 启用: {config.get('user_mention_enabled', False)}\n")
    else:
        print(f"❌ 失败: {resp.text}\n")


def main():
    """运行所有测试"""
    print("\n" + "=" * 60)
    print("  Adidas Monitor 系统 - API 测试")
    print("=" * 60 + "\n")

    try:
        # 1. 健康检查
        test_health()

        # 2. 检查 Slack 配置
        test_slack_config()

        # 3. 添加监控项
        watch_ids = test_add_watch_items()

        # 4. 列出所有监控项
        test_list_watch_items()

        # 5. 获取通知历史
        test_get_notifications()

        # 6. 禁用一个监控项
        if watch_ids:
            test_disable_watch_item(watch_ids[0])

            # 7. 重新启用
            test_enable_watch_item(watch_ids[0])

        # 8. 手动触发扫描（后台执行）
        test_trigger_scan()

        # 等待几秒让扫描执行
        print("⏳ 等待 5 秒让后台扫描执行...\n")
        time.sleep(5)

        # 9. 再次查看通知历史
        test_get_notifications()

        # 10. 删除一个监控项
        if watch_ids:
            test_delete_watch_item(watch_ids[-1])

        # 11. 最终的监控项列表
        test_list_watch_items()

        print("=" * 60)
        print("  ✅ 测试完成！")
        print("=" * 60 + "\n")

    except requests.exceptions.ConnectionError:
        print("❌ 错误：无法连接到服务器")
        print("请确保服务已启动：python main.py")
    except Exception as e:
        print(f"❌ 发生错误: {e}")


if __name__ == "__main__":
    main()
