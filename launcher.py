"""
Dashboard Launcher
Choose between Main Dashboard and Real-Time Trading Dashboard
"""
import subprocess
import sys
import os
from pathlib import Path


def main():
    print("=" * 60)
    print("🎯 Intraday Trade Advisor - Dashboard Launcher")
    print("=" * 60)
    print()
    print("Select a dashboard:")
    print("  1️⃣  Main Dashboard (Complete Analysis)")
    print("      └─ Full watchlist analysis, screening, backtesting")
    print()
    print("  2️⃣  Trading Dashboard (Real-Time Trades) ⚡ NEW")
    print("      └─ Live trade signals, combined analysis, zero latency")
    print()
    print("  3️⃣  Standard HTTP Server (No UI Dependencies)")
    print("      └─ Lightweight server for API access")
    print()
    
    choice = input("Enter your choice (1, 2, or 3): ").strip()
    
    if choice == "1":
        print("\n🚀 Launching Main Dashboard...")
        print("   Opening at http://localhost:8501")
        subprocess.run([sys.executable, "-m", "streamlit", "run", "app.py"], cwd=Path(__file__).parent)
    
    elif choice == "2":
        print("\n⚡ Launching Real-Time Trading Dashboard...")
        print("   Opening at http://localhost:8501")
        subprocess.run([sys.executable, "-m", "streamlit", "run", "trading_dashboard.py"], cwd=Path(__file__).parent)
    
    elif choice == "3":
        print("\n🔧 Starting HTTP Server...")
        print("   Opening at http://localhost:8501")
        subprocess.run([sys.executable, "serve.py"], cwd=Path(__file__).parent)
    
    else:
        print("❌ Invalid choice. Please run again and select 1, 2, or 3.")
        sys.exit(1)


if __name__ == "__main__":
    main()
