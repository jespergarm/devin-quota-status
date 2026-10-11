// Devin Quota Status — live daily/weekly quota % in the status bar.
// Spawns quota.py (stdlib only) which reads the Desktop session token
// and polls api.devin.ai/{org}/billing/quota/usage.

const vscode = require("vscode");
const cp = require("child_process");
const fs = require("fs");
const path = require("path");

let item = null;
let timer = null;
let extContext = null;

function cfg() {
    return vscode.workspace.getConfiguration("devinQuota");
}

// --- Python discovery -------------------------------------------------
// quota.py is stdlib-only — any Python 3 interpreter works. The
// configured devinQuota.pythonPath is tried first, then python/python3/
// py on PATH and the common install roots. The Windows Store "python"
// alias stub is filtered out by the --version probe (it prints a store
// message and exits non-zero).
let pythonBin = null;

function pythonCandidates() {
    const out = [];
    const push = (p) => p && !out.includes(p) && out.push(p);
    push(cfg().get("pythonPath", ""));
    for (const name of ["python", "python3", "py"]) {
        push(name);
    }
    const roots = [
        process.env.LOCALAPPDATA &&
            path.join(process.env.LOCALAPPDATA, "Programs", "Python"),
        process.env.ProgramFiles &&
            path.join(process.env.ProgramFiles, "Python"),
        "C:\\Python",
        "C:\\",
    ].filter(Boolean);
    for (const root of roots) {
        try {
            for (const d of fs.readdirSync(root)) {
                if (/^python3/i.test(d)) {
                    push(path.join(root, d, "python.exe"));
                }
            }
        } catch { /* root does not exist */ }
    }
    return out;
}

function probe(bin) {
    try {
        const r = cp.spawnSync(bin, ["--version"],
            { timeout: 5000, windowsHide: true });
        return r.status === 0
            && /^Python 3\./.test(String(r.stdout).trim());
    } catch {
        return false;
    }
}

function resolvePython() {
    if (pythonBin) {
        return pythonBin;
    }
    for (const cand of pythonCandidates()) {
        if (probe(cand)) {
            pythonBin = cand;
            break;
        }
    }
    return pythonBin;
}

function logPath() {
    const p = (cfg().get("logPath", "") || "").trim();
    if (/^(off|none|disabled)$/i.test(p)) {
        return "";
    }
    return p || path.join(
        extContext.globalStorageUri.fsPath, "quota_pct_log.csv");
}

function fmtReset(iso) {
    const d = iso ? new Date(iso) : null;
    return d && !isNaN(d) ? d.toLocaleString() : "?";
}

const WDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

function fmtTime(iso) {
    const d = iso ? new Date(iso) : null;
    return d && !isNaN(d) ? String(d.getHours()) : "?";
}

function fmtDayTime(iso) {
    const d = iso ? new Date(iso) : null;
    if (!d || isNaN(d)) {
        return "?";
    }
    return `${WDAYS[d.getDay()]} ${fmtTime(iso)}`;
}

function showQuota(q) {
    const d = q.daily_pct;
    const w = q.weekly_pct;
    item.text = `$(pulse) Day ${d}% (${fmtDayTime(q.daily_reset_at)})`
        + ` · Week ${w}% (${fmtDayTime(q.weekly_reset_at)})`;
    const md = new vscode.MarkdownString();
    md.appendMarkdown("**Devin quota** (live from api.devin.ai)\n\n");
    md.appendMarkdown(`Day: ${d}% — resets ${fmtReset(q.daily_reset_at)}\n\n`);
    md.appendMarkdown(`Week: ${w}% — resets ${fmtReset(q.weekly_reset_at)}\n\n`);
    const bal = Number(q.overage_balance);
    md.appendMarkdown(
        `Extra usage balance: $${(isNaN(bal) ? 0 : bal).toFixed(2)}\n\n`);
    md.appendMarkdown("Updated " + new Date().toLocaleTimeString(
        undefined, { hour: "2-digit", minute: "2-digit" }) + "\n\n");
    md.appendMarkdown("_Click to refresh_");
    item.tooltip = md;
    const warnPct = Number(cfg().get("warnPercent", 90)) || 90;
    if (d >= 100 || w >= 100) {
        item.backgroundColor =
            new vscode.ThemeColor("statusBarItem.errorBackground");
    } else if (d >= warnPct || w >= warnPct) {
        item.backgroundColor =
            new vscode.ThemeColor("statusBarItem.warningBackground");
    } else {
        item.backgroundColor = undefined;
    }
    checkWarn(d, w, warnPct);
}

// One notification per threshold crossing per scope (day/week) — re-arms
// when the value drops back below the threshold (e.g. after a reset).
const warned = { d: false, w: false };

function checkWarn(d, w, warnPct) {
    if (!cfg().get("warnEnabled", true)) {
        warned.d = warned.w = false;
        return;
    }
    for (const [key, pct, label] of [["d", d, "Day"], ["w", w, "Week"]]) {
        if (pct >= warnPct && !warned[key]) {
            warned[key] = true;
            const msg = pct >= 100
                ? `Devin quota: ${label} exhausted (${pct}%) — sessions pause until reset.`
                : `Devin quota: ${label} is at ${pct}% (threshold ${warnPct}%).`;
            const show = pct >= 100 ? vscode.window.showErrorMessage
                : vscode.window.showWarningMessage;
            show(msg, "Open usage page").then((pick) => {
                if (pick) {
                    vscode.env.openExternal(
                        vscode.Uri.parse("https://app.devin.ai/settings/usage"));
                }
            });
        } else if (pct < warnPct) {
            warned[key] = false;
        }
    }
}

function showError(msg) {
    item.text = "$(warning) Devin quota: n/a";
    const md = new vscode.MarkdownString();
    md.appendMarkdown("**Devin kvote**\n\n");
    md.appendMarkdown("Error: " + String(msg).slice(0, 300) + "\n\n");
    md.appendMarkdown("_Click to retry_");
    item.tooltip = md;
    item.backgroundColor = undefined;
}

let busy = false;

function refresh(showSpinner) {
    if (!item || busy) {
        return;
    }
    busy = true;
    if (showSpinner) {
        item.text = "$(sync~spin) Devin quota…";
    }
    const bin = resolvePython();
    if (!bin) {
        busy = false;
        showError("no working Python 3 found — set devinQuota.pythonPath");
        return;
    }
    const script = path.join(extContext.extensionUri.fsPath, "quota.py");
    cp.execFile(
        bin,
        [script, extContext.globalStorageUri.fsPath, logPath()],
        { timeout: 20000, windowsHide: true },
        (err, stdout) => {
            busy = false;
            if (err) {
                pythonBin = null;
                showError(err.message);
                return;
            }
            let q;
            try {
                q = JSON.parse(stdout.trim());
            } catch (e) {
                showError("bad output: " + stdout.trim().slice(0, 120));
                return;
            }
            if (q.error || q.daily_pct == null || q.weekly_pct == null) {
                showError(q.error || "no data");
                return;
            }
            showQuota(q);
        });
}

function armTimer() {
    if (timer) {
        clearInterval(timer);
    }
    const secs = Math.max(10, Number(cfg().get("intervalSeconds", 30)) || 30);
    timer = setInterval(() => refresh(false), secs * 1000);
}

function activate(context) {
    extContext = context;
    if (item) {
        return;  // already running in this extension host
    }
    try {
        context.subscriptions.push(
            vscode.commands.registerCommand("devinQuota.refresh",
                () => refresh(true)));
    } catch (e) {
        // An older version of this extension may still be active in this
        // window and already owns the command (hot-install of a VSIX
        // upgrade). Keep going — this instance still polls and displays
        // fine; clicks just hit the old handler until the window reloads.
    }
    item = vscode.window.createStatusBarItem(
        vscode.StatusBarAlignment.Right, -1000);
    item.command = "devinQuota.refresh";
    item.text = "$(sync~spin) Devin quota…";
    item.show();
    context.subscriptions.push(item);
    context.subscriptions.push(
        vscode.workspace.onDidChangeConfiguration((e) => {
            if (e.affectsConfiguration("devinQuota")) {
                armTimer();
            }
        }));
    context.subscriptions.push(
        vscode.window.onDidChangeWindowState((s) => {
            if (s.focused) {
                refresh(false);
            }
        }));
    context.subscriptions.push({ dispose: () => clearInterval(timer) });
    armTimer();
    refresh(true);
}

function deactivate() {
    if (timer) {
        clearInterval(timer);
    }
}

module.exports = { activate, deactivate };
