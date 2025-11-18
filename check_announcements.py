"""检查公告收集状态"""

import requests
import sys

def check_announcements():
    """检查公告数据"""
    try:
        # 检查API是否可访问
        response = requests.get("http://localhost:8000/api/announcements", timeout=5)
        response.raise_for_status()
        announcements = response.json()
        
        print("=" * 60)
        print("公告数据检查 / Announcement Data Check")
        print("=" * 60)
        print(f"总公告数量 / Total Announcements: {len(announcements)}")
        print()
        
        if len(announcements) == 0:
            print("[警告] 数据库中没有公告数据 / [WARNING] No announcements in database")
            print()
            print("可能的原因 / Possible reasons:")
            print("1. 调度器未启动或公告抓取任务未运行")
            print("   Scheduler not started or announcement polling task not running")
            print("2. 公告抓取失败（网络问题、代理问题等）")
            print("   Announcement fetching failed (network, proxy issues, etc.)")
            print("3. 公告抓取间隔太长（默认90秒），需要等待")
            print("   Polling interval too long (default 90s), need to wait")
            print("4. 币安API返回空数据或格式变化")
            print("   Binance API returned empty data or format changed")
            print()
            print("建议操作 / Recommended actions:")
            print("1. 检查服务日志，查看是否有错误")
            print("   Check service logs for errors")
            print("2. 手动触发公告抓取:")
            print("   Manually trigger announcement fetch:")
            print("   curl -X POST 'http://localhost:8000/api/backfill/announcements?months=1&max_pages=10'")
            print("3. 检查 .env 配置中的 HTTP_PROXY（如果需要）")
            print("   Check HTTP_PROXY in .env (if needed)")
        else:
            print("公告列表 / Announcement List:")
            print("-" * 60)
            for i, ann in enumerate(announcements[:10], 1):
                print(f"{i}. {ann.get('title', 'N/A')[:60]}")
                print(f"   交易对 / Symbol: {ann.get('symbol', 'N/A')}")
                print(f"   状态 / Status: {ann.get('status', 'N/A')}")
                print(f"   来源 / Source: {ann.get('source', 'N/A')}")
                print(f"   创建时间 / Created: {ann.get('created_at', 'N/A')}")
                print()
        
        # 检查调度器状态（通过检查是否有新公告）
        print("=" * 60)
        print("调度器状态检查 / Scheduler Status Check")
        print("=" * 60)
        print("提示: 调度器每90秒自动抓取一次公告")
        print("Tip: Scheduler fetches announcements every 90 seconds")
        print("如果长时间没有新公告，请检查:")
        print("If no new announcements for a long time, check:")
        print("1. 服务是否正常运行")
        print("   Is the service running?")
        print("2. 查看终端日志中的错误信息")
        print("   Check terminal logs for errors")
        print("3. 手动触发抓取测试")
        print("   Manually trigger fetch to test")
        
    except requests.exceptions.ConnectionError:
        print("[错误] 无法连接到服务 / [ERROR] Cannot connect to service")
        print("请确保服务正在运行: http://localhost:8000")
        print("Please ensure service is running: http://localhost:8000")
        sys.exit(1)
    except Exception as e:
        print(f"[错误] 检查失败 / [ERROR] Check failed: {e}")
        sys.exit(1)

if __name__ == "__main__":
    check_announcements()

