' 隐藏窗口运行传入的 cmd 脚本（计划任务动作入口，避免控制台窗口闪现）
Set sh = CreateObject("Wscript.Shell")
sh.Run """" & WScript.Arguments(0) & """", 0, False
