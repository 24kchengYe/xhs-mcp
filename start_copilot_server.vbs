' XHS-MCP 数据接收服务 - 静默后台启动（无弹窗）
' 端口：3081 - 接收社媒助手的数据上报
Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = "D:\pythonPycharms\xhs-mcp"
WshShell.Run "python server.py --http-only", 0, False
