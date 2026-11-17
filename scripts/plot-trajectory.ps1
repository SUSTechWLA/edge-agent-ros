param(
    [Parameter(Mandatory, Position=0)][string] $EvidenceDir,
    [string] $OutputFile
)
$ErrorActionPreference = 'Stop'
$folder = (Resolve-Path -LiteralPath $EvidenceDir).ProviderPath
if (-not $OutputFile) { $OutputFile = Join-Path $folder 'mission-trajectory.png' }
$outputPath = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($OutputFile)

# WinForms Chart ships with .NET Framework on Windows. Keep the renderer usable
# from both Windows PowerShell and the newer pwsh shell without extra packages.
if ($PSVersionTable.PSEdition -eq 'Core') {
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $PSCommandPath -EvidenceDir $folder -OutputFile $outputPath
    exit $LASTEXITCODE
}
Add-Type -AssemblyName System.Windows.Forms.DataVisualization
Add-Type -AssemblyName System.Drawing
$mission = Get-Content -LiteralPath (Join-Path $folder 'mission.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$samples = Get-Content -LiteralPath (Join-Path $folder 'mission.trajectory.json') -Raw -Encoding UTF8 | ConvertFrom-Json
if ($mission.passed -ne $true -or @($mission.world_witnesses).Count -ne 2 -or $samples.Count -lt 2) {
    throw 'Expected a passing two-waypoint mission and at least two Gazebo world-pose samples.'
}

function New-Layer($name, $type, $color, $marker, $size) {
    $s = New-Object System.Windows.Forms.DataVisualization.Charting.Series $name
    $s.ChartType = $type
    $s.Color = [Drawing.ColorTranslator]::FromHtml($color)
    $s.MarkerStyle = $marker
    $s.MarkerSize = $size
    $s.ChartArea = 'World'
    $s.Legend = 'Key'
    $chart.Series.Add($s)
    return $s
}
function Add-Point($series, $x, $y) {
    if ($null -eq $x -or $null -eq $y -or [double]::IsNaN($x) -or [double]::IsNaN($y) -or
        [double]::IsInfinity($x) -or [double]::IsInfinity($y)) { throw 'Invalid XY coordinate.' }
    return $series.Points.AddXY([double]$x, [double]$y)
}
function New-Position($x, $y, $w, $h) {
    return New-Object System.Windows.Forms.DataVisualization.Charting.ElementPosition $x,$y,$w,$h
}
function Add-Title($message, $fontSize, $bold, $x, $y, $w, $h) {
    $title = New-Object System.Windows.Forms.DataVisualization.Charting.Title
    $title.Text = $message
    $fontStyle = if ($bold) { [Drawing.FontStyle]::Bold } else { [Drawing.FontStyle]::Regular }
    $title.Font = New-Object Drawing.Font 'Segoe UI',$fontSize,$fontStyle
    $title.Alignment = [Drawing.ContentAlignment]::TopLeft
    $title.Position = New-Position $x $y $w $h
    $title.ForeColor = [Drawing.ColorTranslator]::FromHtml('#1b3f45')
    $chart.Titles.Add($title)
}

$chart = New-Object System.Windows.Forms.DataVisualization.Charting.Chart
try {
    $chart.Width = 1150; $chart.Height = 850
    $chart.BackColor = [Drawing.Color]::White
    $chart.AntiAliasing = 'All'
    $plot = New-Object System.Windows.Forms.DataVisualization.Charting.ChartArea 'World'
    $chart.ChartAreas.Add($plot)
    $plot.Position = New-Position 0 0 100 100
    # InnerPlotPosition is expressed as percentages of the chart area. Fix both
    # pixel dimensions to 640 so one metre has the same length along X and Y.
    $plot.InnerPlotPosition = New-Position 9 14 (100*640/$chart.Width) (100*640/$chart.Height)
    $legend = New-Object System.Windows.Forms.DataVisualization.Charting.Legend 'Key'
    $legend.Font = New-Object Drawing.Font 'Segoe UI',11
    $legend.LegendStyle = 'Column'
    $legend.Position = New-Position 68 22 31 19
    $chart.Legends.Add($legend)
    $track = New-Layer 'Gazebo world trajectory' 'Line' '#13786d' 'None' 1
    $track.BorderWidth = 3
    foreach ($p in $samples) { $null = Add-Point $track $p.x $p.y }
    $goals = New-Layer 'Requested waypoint' 'Point' '#b54b26' 'Cross' 14
    $stops = New-Layer 'Observed stopped pose' 'Point' '#19489a' 'Circle' 9
    $goals.Font = New-Object Drawing.Font 'Segoe UI',10,([Drawing.FontStyle]::Bold)
    $goals.SmartLabelStyle.Enabled = $false
    $goals['LabelStyle'] = 'TopRight'
    $detail = @()
    for ($i = 0; $i -lt 2; $i++) {
        $point = $mission.world_witnesses[$i]
        $label = @('A','B')[$i]
        $idx = Add-Point $goals $point.goal[0] $point.goal[1]
        $goals.Points[$idx].Label = $label
        $null = Add-Point $stops $point.world[0] $point.world[1]
        $detail += ('{0}    target ({1:F2}, {2:F2}) m' -f $label,$point.goal[0],$point.goal[1])
        $detail += ('       observed ({0:F3}, {1:F3}) m' -f $point.world[0],$point.world[1])
        $detail += ('       error {0:F3} m' -f $point.error_m)
        $detail += ''
    }
    $xs = @(foreach ($s in $chart.Series) { foreach ($p in $s.Points) { $p.XValue } })
    $ys = @(foreach ($s in $chart.Series) { foreach ($p in $s.Points) { $p.YValues[0] } })
    $ex = $xs | Measure-Object -Minimum -Maximum
    $ey = $ys | Measure-Object -Minimum -Maximum
    $range = [math]::Max(0.1, [math]::Max($ex.Maximum-$ex.Minimum, $ey.Maximum-$ey.Minimum))
    $interval = [math]::Pow(10, [math]::Floor([math]::Log10($range/4)))
    if ($range/$interval -gt 15) { $interval *= 5 }
    elseif ($range/$interval -gt 8) { $interval *= 2 }
    $left = [math]::Floor(($ex.Minimum - 0.08*$range)/$interval)*$interval
    $bottom = [math]::Floor(($ey.Minimum - 0.08*$range)/$interval)*$interval
    $side = [math]::Ceiling(([math]::Max($ex.Maximum-$left,$ey.Maximum-$bottom)+0.1*$range)/$interval)*$interval
    $plot.AxisX.Minimum = $left; $plot.AxisX.Maximum = $left+$side; $plot.AxisX.Title = 'World X (m)'
    $plot.AxisY.Minimum = $bottom; $plot.AxisY.Maximum = $bottom+$side; $plot.AxisY.Title = 'World Y (m)'
    foreach ($axis in @($plot.AxisX,$plot.AxisY)) {
        $axis.Interval = $interval
        $axis.MajorGrid.LineColor = [Drawing.ColorTranslator]::FromHtml('#d9e3e2')
        $axis.LabelStyle.Font = New-Object Drawing.Font 'Segoe UI',10
        $axis.LabelStyle.Format = '0.##'
        $axis.TitleFont = New-Object Drawing.Font 'Segoe UI',12
    }
    Add-Title "Mission $($mission.mission_id) | Gazebo world trajectory" 17 $true 7 3 91 7
    Add-Title 'Stationary observations' 12 $true 69 45 30 7
    Add-Title ($detail -join [Environment]::NewLine) 11 $false 69 51 30 34
    $chart.SaveImage($outputPath, [System.Windows.Forms.DataVisualization.Charting.ChartImageFormat]::Png)
    Write-Output $outputPath
} finally {
    $chart.Dispose()
}
