Const POLL_MS = 1200

Dim fso, shell, wmi
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
Set wmi = GetObject("winmgmts:{impersonationLevel=impersonate}!\\.\root\cimv2")

Dim appDataDir, settingsFile, petPathFile, startBatPathFile, stopBatPathFile, tokenFile, lockFile, wakeReasonFile, scriptDir
appDataDir = shell.ExpandEnvironmentStrings("%APPDATA%") & "\GeminiDesktopPet"
If Not fso.FolderExists(appDataDir) Then fso.CreateFolder(appDataDir)

settingsFile = appDataDir & "\settings.json"
petPathFile = appDataDir & "\pet_path.txt"
startBatPathFile = appDataDir & "\start_bat_path.txt"
stopBatPathFile = appDataDir & "\stop_bat_path.txt"
tokenFile = appDataDir & "\watcher_token.txt"
lockFile = appDataDir & "\watcher.lock"
wakeReasonFile = appDataDir & "\wake_reason.txt"
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)

Dim isAutoMode, arg
isAutoMode = False
For Each arg In WScript.Arguments
    If LCase(arg) = "/auto" Then isAutoMode = True
Next

Randomize Timer
Dim myToken
myToken = CStr(CLng(Timer * 1000)) & "_" & CStr(CLng(Rnd() * 1000000))
Call WriteAllAscii(tokenFile, myToken)
WScript.Sleep 180

Dim lockStream, lockTry
Set lockStream = Nothing
For lockTry = 1 To 15
    Err.Clear
    Set lockStream = fso.OpenTextFile(lockFile, 2, True)
    If Err.Number = 0 And Not (lockStream Is Nothing) Then Exit For
    Set lockStream = Nothing
    WScript.Sleep 200
Next
Err.Clear

Dim followAg, followGemini, autoClose
Call ReadFollowFlags(followAg, followGemini, autoClose)

Dim wasAg, wasGemini, petRunningAtStart
petRunningAtStart = IsProcessRunning("GeminiDesktopPet.exe")
If petRunningAtStart Or isAutoMode Then
    wasAg = IsProcessRunning("Antigravity.exe")
    wasGemini = IsProcessRunning("Gemini.exe")
Else
    wasAg = False
    wasGemini = False
End If

Dim pendingClose, exitStreak
pendingClose = False
exitStreak = 0

Dim currentToken, isAg, isGemini, triggerReason, startBat, stopBat, resolvedExe
Dim wasAnyActive, anyActiveNow
Do While True
    currentToken = TrimText(ReadAllTextSafe(tokenFile))
    If currentToken = "STOP" Then Exit Do
    If currentToken <> "" And currentToken <> myToken Then Exit Do

    Call ReadFollowFlags(followAg, followGemini, autoClose)
    If (Not followAg) And (Not followGemini) Then Exit Do

    isAg = IsProcessRunning("Antigravity.exe")
    isGemini = IsProcessRunning("Gemini.exe")
    triggerReason = ""

    If followAg And isAg And Not wasAg Then
        triggerReason = "antigravity"
    ElseIf followGemini And isGemini And Not wasGemini Then
        triggerReason = "gemini"
    End If

    wasAnyActive = (followAg And wasAg) Or (followGemini And wasGemini)
    anyActiveNow = (followAg And isAg) Or (followGemini And isGemini)

    If autoClose And wasAnyActive And (Not anyActiveNow) Then
        pendingClose = True
        exitStreak = 1
    ElseIf pendingClose Then
        If anyActiveNow Or (Not autoClose) Then
            pendingClose = False
            exitStreak = 0
        Else
            exitStreak = exitStreak + 1
            If exitStreak >= 2 Then
                pendingClose = False
                exitStreak = 0
                If IsProcessRunning("GeminiDesktopPet.exe") Then
                    stopBat = ResolveStopBat()
                    If stopBat <> "" Then
                        Err.Clear
                        shell.Run """" & stopBat & """", 0, False
                    End If
                End If
            End If
        End If
    End If

    wasAg = isAg
    wasGemini = isGemini

    If triggerReason <> "" Then
        pendingClose = False
        exitStreak = 0
        If Not IsProcessRunning("GeminiDesktopPet.exe") Then
            Call WriteAllAscii(wakeReasonFile, triggerReason)
            startBat = ResolveStartBat()
            If startBat <> "" Then
                Err.Clear
                shell.Run """" & startBat & """ --woken-by:" & triggerReason, 0, False
            Else
                resolvedExe = ResolvePetExe()
                If resolvedExe <> "" Then
                    Err.Clear
                    shell.Run """" & resolvedExe & """ --woken-by:" & triggerReason, 1, False
                End If
            End If
        End If
    End If

    WScript.Sleep POLL_MS
Loop

If Not (lockStream Is Nothing) Then lockStream.Close
WScript.Quit 0

Sub ReadFollowFlags(ByRef outAg, ByRef outGemini, ByRef outAutoClose)
    Dim jsonText, compact
    If Not fso.FileExists(settingsFile) Then
        outAg = True
        outGemini = True
        outAutoClose = False
        Exit Sub
    End If
    jsonText = ReadAllTextSafe(settingsFile)
    If jsonText = "" Then
        WScript.Sleep 80
        jsonText = ReadAllTextSafe(settingsFile)
    End If
    If jsonText = "" Then
        outAg = False
        outGemini = False
        outAutoClose = False
        Exit Sub
    End If
    compact = Replace(Replace(Replace(Replace(jsonText, " ", ""), vbTab, ""), vbCr, ""), vbLf, "")
    outAg = (InStr(1, compact, """FollowAntigravity"":true", 1) > 0)
    outGemini = (InStr(1, compact, """FollowGeminiApp"":true", 1) > 0)
    outAutoClose = (InStr(1, compact, """SleepWhenAntigravityExits"":true", 1) > 0)
End Sub

Function IsProcessRunning(procName)
    Dim colProcs, item
    IsProcessRunning = False
    Err.Clear
    Set colProcs = wmi.ExecQuery("SELECT ProcessId FROM Win32_Process WHERE Name='" & procName & "'")
    If Err.Number <> 0 Then
        Err.Clear
        Set wmi = GetObject("winmgmts:{impersonationLevel=impersonate}!\\.\root\cimv2")
        Exit Function
    End If
    For Each item In colProcs
        IsProcessRunning = True
        Exit Function
    Next
End Function

Function ResolveStartBat()
    Dim candidates(3), idx, petExe, petDir
    candidates(0) = DEFAULT_START_BAT
    candidates(1) = TrimText(ReadAllTextSafe(startBatPathFile))
    candidates(2) = scriptDir & "\StartPet.bat"
    petExe = ResolvePetExe()
    If petExe <> "" Then
        petDir = fso.GetParentFolderName(petExe)
        candidates(3) = petDir & "\Scripts\StartPet.bat"
    Else
        candidates(3) = ""
    End If
    ResolveStartBat = PickExistingFile(candidates, 3)
End Function

Function ResolveStopBat()
    Dim candidates(3), idx, petExe, petDir
    candidates(0) = DEFAULT_STOP_BAT
    candidates(1) = TrimText(ReadAllTextSafe(stopBatPathFile))
    candidates(2) = scriptDir & "\StopPet.bat"
    petExe = ResolvePetExe()
    If petExe <> "" Then
        petDir = fso.GetParentFolderName(petExe)
        candidates(3) = petDir & "\Scripts\StopPet.bat"
    Else
        candidates(3) = ""
    End If
    ResolveStopBat = PickExistingFile(candidates, 3)
End Function

Function ResolvePetExe()
    Dim candidates(3)
    candidates(0) = DEFAULT_PET_EXE
    candidates(1) = TrimText(ReadAllTextSafe(petPathFile))
    candidates(2) = scriptDir & "\GeminiDesktopPet.exe"
    candidates(3) = ""
    ResolvePetExe = PickExistingFile(candidates, 3)
End Function

Function PickExistingFile(arr, maxIdx)
    Dim i
    PickExistingFile = ""
    For i = 0 To maxIdx
        If arr(i) <> "" Then
            If fso.FileExists(arr(i)) Then
                PickExistingFile = arr(i)
                Exit Function
            End If
        End If
    Next
End Function

Function ReadAllTextSafe(filePath)
    Dim s
    ReadAllTextSafe = ""
    If Not fso.FileExists(filePath) Then Exit Function
    Err.Clear
    Set s = fso.OpenTextFile(filePath, 1, False)
    If Err.Number = 0 And Not (s Is Nothing) Then
        If Not s.AtEndOfStream Then ReadAllTextSafe = s.ReadAll
        s.Close
    End If
    Err.Clear
End Function

Sub WriteAllAscii(filePath, content)
    Dim s
    Err.Clear
    Set s = fso.OpenTextFile(filePath, 2, True)
    If Err.Number = 0 And Not (s Is Nothing) Then
        s.Write content
        s.Close
    End If
    Err.Clear
End Sub

Function TrimText(txt)
    txt = Replace(txt, vbCr, "")
    txt = Replace(txt, vbLf, "")
    TrimText = Trim(txt)
End Function
