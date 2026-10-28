"""Render the shared native event-mode download transports."""

from __future__ import annotations


def render_shell_download_helpers(*, publisher: bool = False) -> str:
    """Render the event-only shell downloader for one of its two callers."""

    if publisher:
        download_command = r"""  (
    # The shell, prlimit and curl replace one another in this same child PID.
    # Ignoring XFSZ makes a capped write fail without a signal core dump.
    trap '' XFSZ
    exec prlimit --fsize="$((DOWNLOAD_EXPECTED_SIZE + 1)):$((DOWNLOAD_EXPECTED_SIZE + 1))" --core=0:0 -- \
      curl --disable --fail --location --silent --show-error --proto '=https' --proto-redir '=https' --tlsv1.2 \
      --connect-timeout 30 --max-time 900 --max-redirs 8 --max-filesize "$DOWNLOAD_EXPECTED_SIZE" \
      -o "$DOWNLOAD_PATH" "$DOWNLOAD_URL"
  ) &"""
    else:
        download_command = r"""  curl --fail --location --silent --show-error --connect-timeout 30 --max-time 900 \
    --proto '=https' --proto-redir '=https' --tlsv1.2 \
    -o "$DOWNLOAD_PATH" "$DOWNLOAD_URL" &"""

    return r"""
stop_bootstrap_download() {
  # This owns only the current downloader child, never an unpacker or retained PID.
  if [ -z "$BOOTSTRAP_DOWNLOAD_PID" ] || [ "$BOOTSTRAP_NATIVE_ACTIVE" -ne 1 ]; then return 0; fi
  STOP_DOWNLOAD_PID=$BOOTSTRAP_DOWNLOAD_PID
  kill -TERM "$STOP_DOWNLOAD_PID" 2>/dev/null || :
  STOP_DOWNLOAD_POLLS=0
  while [ "$STOP_DOWNLOAD_POLLS" -lt 20 ]; do
    if ! kill -0 "$STOP_DOWNLOAD_PID" 2>/dev/null; then break; fi
    sleep 0.1
    STOP_DOWNLOAD_POLLS=$((STOP_DOWNLOAD_POLLS + 1))
  done
  if kill -0 "$STOP_DOWNLOAD_PID" 2>/dev/null; then
    kill -KILL "$STOP_DOWNLOAD_PID" 2>/dev/null || :
  fi
  wait "$STOP_DOWNLOAD_PID" || :
  BOOTSTRAP_DOWNLOAD_PID='' BOOTSTRAP_NATIVE_ACTIVE=0
}

download_https_event() {
  DOWNLOAD_URL=$1
  DOWNLOAD_PATH=$2
  DOWNLOAD_EXPECTED_SIZE=${3:-0}
  check_download_cancellation
  BOOTSTRAP_NATIVE_ACTIVE=1
@@DOWNLOAD_COMMAND@@
  BOOTSTRAP_DOWNLOAD_PID=$!
  DOWNLOAD_PID=$BOOTSTRAP_DOWNLOAD_PID
  DOWNLOAD_LAST_BYTES=0
  while kill -0 "$DOWNLOAD_PID" 2>/dev/null; do
    check_download_cancellation
    if [ "$DOWNLOAD_EXPECTED_SIZE" -gt 0 ] && [ -f "$DOWNLOAD_PATH" ] && [ ! -L "$DOWNLOAD_PATH" ]; then
      DOWNLOAD_BYTES=$(wc -c < "$DOWNLOAD_PATH" | tr -d '[:space:]')
      case "$DOWNLOAD_BYTES" in ''|*[!0-9]*) DOWNLOAD_BYTES=0 ;; esac
      if [ "$DOWNLOAD_BYTES" -gt "$DOWNLOAD_EXPECTED_SIZE" ]; then
        mark_download_size_failure
        stop_bootstrap_download
        exit 1
      fi
      if [ "$DOWNLOAD_BYTES" -gt "$DOWNLOAD_LAST_BYTES" ] && [ "$DOWNLOAD_BYTES" -lt "$DOWNLOAD_EXPECTED_SIZE" ]; then
        report_download_progress "$DOWNLOAD_BYTES" "$DOWNLOAD_EXPECTED_SIZE"
        DOWNLOAD_LAST_BYTES=$DOWNLOAD_BYTES
      fi
    fi
    sleep 0.2
  done
  if wait "$DOWNLOAD_PID"; then DOWNLOAD_EXIT=0; else DOWNLOAD_EXIT=$?; fi
  BOOTSTRAP_DOWNLOAD_PID='' BOOTSTRAP_NATIVE_ACTIVE=0
  return "$DOWNLOAD_EXIT"
}
""".replace("@@DOWNLOAD_COMMAND@@", download_command)


def render_powershell_download_helpers(*, publisher: bool = False) -> str:
    """Render event-mode HTTP reads for the carrier or publisher bootstrap."""

    if publisher:
        initial_url_check = """if ($initialUri.Scheme -cne 'https' -or $initialUri.UserInfo) {
        throw 'Dependency downloads require HTTPS without credentials.'
    }"""
        redirect_url_check = """if ($nextUri.Scheme -cne 'https' -or $nextUri.UserInfo) {
                    throw 'Dependency downloads require HTTPS without credentials.'
                }"""
        response_policy = """if (-not $response.IsSuccessStatusCode -or $statusCode -ne 200) {
                throw 'A dependency publisher did not return the requested file.'
            }"""
        content_length_policy = """if ($null -ne $contentLength -and [long]$contentLength -ge 0 -and [long]$contentLength -ne $ExpectedSize) {
            throw 'A dependency publisher returned an unexpected file size.'
        }"""
        user_agent = "[void]$client.DefaultRequestHeaders.UserAgent.ParseAdd('Carlina-Documents-publisher-bootstrap/1')"
        read_length = "$readLength = [int][Math]::Min($buffer.Length, $ExpectedSize - $bytesCompleted + 1)"
        flush = "$outputStream.Flush($true)"
        completion_policy = """if ($bytesCompleted -ne $ExpectedSize) {
            throw 'A dependency download ended before its expected size.'
        }"""
        redirect_limit = "8"
    else:
        initial_url_check = (
            """if ($initialUri.Scheme -cne 'https') { throw 'HTTPS is required.' }"""
        )
        redirect_url_check = """if ($nextUri.Scheme -cne 'https') { throw 'Download redirect left HTTPS.' }"""
        response_policy = """if (-not $response.IsSuccessStatusCode) {
                throw ('Download returned HTTP status ' + $statusCode + '.')
            }"""
        content_length_policy = """if ($ExpectedSize -gt 0 -and $null -ne $contentLength -and [long]$contentLength -gt $ExpectedSize) {
            Set-DownloadSizeFailure
            throw 'Downloaded artifact exceeds its expected size.'
        }"""
        user_agent = ""
        read_length = "$readLength = $buffer.Length"
        flush = "$outputStream.Flush()"
        completion_policy = ""
        redirect_limit = "10"

    return (
        r"""
function Wait-InstallationDownloadTask(
    [System.Threading.Tasks.Task]$Task,
    [System.Threading.CancellationTokenSource]$CancellationSource,
    [System.Diagnostics.Stopwatch]$Clock,
    [int]$TimeoutSeconds
) {
    while (-not $Task.IsCompleted) {
        Invoke-DownloadCancellationCheck
        if ($Clock.Elapsed.TotalSeconds -ge $TimeoutSeconds) {
            $CancellationSource.Cancel()
            throw [TimeoutException]::new('Download exceeded its time limit.')
        }
        Start-Sleep -Milliseconds 100
    }
    Invoke-DownloadCancellationCheck
    if ($Clock.Elapsed.TotalSeconds -ge $TimeoutSeconds) {
        $CancellationSource.Cancel()
        throw [TimeoutException]::new('Download exceeded its time limit.')
    }
    return $Task.GetAwaiter().GetResult()
}

function Save-HttpsEventFile([string]$Url, [string]$Path, [long]$ExpectedSize = 0) {
    $downloadTimeoutSeconds = 900
    $initialUri = [Uri]$Url
    @@INITIAL_URL_CHECK@@
    Add-Type -AssemblyName System.Net.Http
    $clock = [System.Diagnostics.Stopwatch]::StartNew()
    $cancellationSource = [System.Threading.CancellationTokenSource]::new()
    $cancellationSource.CancelAfter([TimeSpan]::FromSeconds($downloadTimeoutSeconds))
    $handler = $null
    $client = $null
    $response = $null
    $inputStream = $null
    $outputStream = $null
    $pendingTask = $null
    try {
        $handler = [System.Net.Http.HttpClientHandler]::new()
        $handler.AllowAutoRedirect = $false
        $client = [System.Net.Http.HttpClient]::new($handler)
        $client.Timeout = [TimeSpan]::FromMilliseconds(-1)
        @@USER_AGENT@@
        $currentUri = $initialUri
        $redirectCount = 0
        while ($true) {
            $pendingTask = $client.GetAsync(
                $currentUri,
                [System.Net.Http.HttpCompletionOption]::ResponseHeadersRead,
                $cancellationSource.Token
            )
            $response = Wait-InstallationDownloadTask $pendingTask $cancellationSource $clock $downloadTimeoutSeconds
            $pendingTask = $null
            $statusCode = [int]$response.StatusCode
            if ($statusCode -in @(301, 302, 303, 307, 308)) {
                if ($redirectCount -ge @@REDIRECT_LIMIT@@) { throw 'Download exceeded its redirect limit.' }
                $location = $response.Headers.Location
                if ($null -eq $location) { throw 'Download redirect has no location.' }
                $nextUri = [Uri]::new($currentUri, $location)
                $response.Dispose()
                $response = $null
                @@REDIRECT_URL_CHECK@@
                $currentUri = $nextUri
                $redirectCount += 1
                continue
            }
            @@RESPONSE_POLICY@@
            break
        }
        $contentLength = $response.Content.Headers.ContentLength
        @@CONTENT_LENGTH_POLICY@@
        $pendingTask = $response.Content.ReadAsStreamAsync()
        $inputStream = Wait-InstallationDownloadTask $pendingTask $cancellationSource $clock $downloadTimeoutSeconds
        $pendingTask = $null
        $outputStream = [System.IO.FileStream]::new(
            $Path, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None
        )
        $buffer = New-Object byte[] 65536
        $bytesCompleted = [long]0
        $lastReportedBytes = [long]0
        $lastProgressAtMilliseconds = [long]0
        while ($true) {
            @@READ_LENGTH@@
            $pendingTask = $inputStream.ReadAsync($buffer, 0, $readLength, $cancellationSource.Token)
            $read = [int](Wait-InstallationDownloadTask $pendingTask $cancellationSource $clock $downloadTimeoutSeconds)
            $pendingTask = $null
            if ($read -eq 0) { break }
            $nextBytesCompleted = $bytesCompleted + [long]$read
            if ($ExpectedSize -gt 0 -and $nextBytesCompleted -gt $ExpectedSize) {
                Set-DownloadSizeFailure
                throw 'Downloaded artifact exceeds its expected size.'
            }
            $outputStream.Write($buffer, 0, $read)
            $bytesCompleted = $nextBytesCompleted
            if (
                $ExpectedSize -gt 0 -and
                $bytesCompleted -gt $lastReportedBytes -and
                $bytesCompleted -lt $ExpectedSize -and
                ($lastReportedBytes -eq 0 -or ($clock.ElapsedMilliseconds - $lastProgressAtMilliseconds) -ge 200)
            ) {
                Send-DownloadProgress $bytesCompleted $ExpectedSize
                $lastReportedBytes = $bytesCompleted
                $lastProgressAtMilliseconds = $clock.ElapsedMilliseconds
            }
        }
        @@COMPLETION_POLICY@@
        @@FLUSH@@
    } finally {
        if ($null -ne $cancellationSource) { try { $cancellationSource.Cancel() } catch {} }
        if ($null -ne $outputStream) { try { $outputStream.Dispose() } catch {} }
        if ($null -ne $inputStream) { try { $inputStream.Dispose() } catch {} }
        if ($null -ne $response) { try { $response.Dispose() } catch {} }
        if ($null -ne $client) { try { $client.Dispose() } catch {} }
        if ($null -ne $handler) { try { $handler.Dispose() } catch {} }
        # Reads target only the private buffer; file writes are synchronous here.
        # Never wait on a cancelled network task or reuse its buffer.
        if ($null -ne $pendingTask -and $pendingTask.IsCompleted) {
            try { $null = $pendingTask.GetAwaiter().GetResult() } catch {}
        }
        if ($null -ne $cancellationSource) { try { $cancellationSource.Dispose() } catch {} }
    }
}
""".replace("@@INITIAL_URL_CHECK@@", initial_url_check)
        .replace("@@REDIRECT_LIMIT@@", redirect_limit)
        .replace("@@REDIRECT_URL_CHECK@@", redirect_url_check)
        .replace("@@RESPONSE_POLICY@@", response_policy)
        .replace("@@CONTENT_LENGTH_POLICY@@", content_length_policy)
        .replace("@@USER_AGENT@@", user_agent)
        .replace("@@READ_LENGTH@@", read_length)
        .replace("@@COMPLETION_POLICY@@", completion_policy)
        .replace("@@FLUSH@@", flush)
    )
