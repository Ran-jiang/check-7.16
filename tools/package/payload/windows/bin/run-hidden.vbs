' Run the given cmd script with a hidden window (scheduled task entry)
Set sh = CreateObject("Wscript.Shell")
sh.Run """" & WScript.Arguments(0) & """", 0, False
