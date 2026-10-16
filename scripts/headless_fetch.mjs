#!/usr/bin/env node
/*
 * Render a public page in headless Chrome and write the rendered HTML.
 *
 * Bundled with EJILTalkSearcher: EJIL: Talk! is a public blog, so no cookies
 * are injected and no session is required.  The script is deliberately
 * generic: navigation, a bounded wait for an optional result marker, then the
 * outer HTML is written to --out.  Parsing belongs to the calling skill.
 *
 * Requires Node.js 22+ (global WebSocket) and Chrome/Chromium/Edge.
 * Usage:
 *   node headless_fetch.mjs --url URL --out FILE [--wait-ms N]
 *        [--result-selector CSS] [--result-text REGEX] [--chrome PATH]
 */

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawn, spawnSync } from "node:child_process";
import process from "node:process";

const DEFAULT_UA =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) " +
  "AppleWebKit/537.36 (KHTML, like Gecko) " +
  "Chrome/155.0.0.0 Safari/537.36";

function parseArgs(argv) {
  const out = {};
  for (let i = 0; i < argv.length; i += 1) {
    const key = argv[i];
    const value = argv[i + 1];
    if (!key.startsWith("--")) continue;
    out[key.slice(2)] = value;
    i += 1;
  }
  return out;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function waitForFile(file, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (fs.existsSync(file)) return;
    await sleep(100);
  }
  throw new Error(`Timed out waiting for ${file}`);
}

async function fetchJson(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`HTTP ${res.status} for ${url}`);
  return res.json();
}

function firstExisting(candidates) {
  for (const candidate of candidates) {
    if (!candidate) continue;
    if (fs.existsSync(candidate)) return candidate;
  }
  return "";
}

function findChrome(explicit) {
  if (explicit) return explicit;
  if (process.env.CHROME_PATH) return process.env.CHROME_PATH;
  if (process.env.CHROME_BIN) return process.env.CHROME_BIN;

  const command = process.platform === "win32" ? "where.exe" : "which";
  for (const name of [
    "chrome",
    "google-chrome",
    "chromium",
    "chromium-browser",
    "msedge",
    "microsoft-edge",
  ]) {
    const result = spawnSync(command, [name], { encoding: "utf8" });
    if (result.status === 0 && result.stdout.trim()) {
      return result.stdout.trim().split(/\r?\n/)[0];
    }
  }

  if (process.platform === "win32") {
    const roots = [
      process.env.ProgramFiles,
      process.env["ProgramFiles(x86)"],
      process.env.LOCALAPPDATA,
    ].filter(Boolean);
    return firstExisting(
      roots.flatMap((root) => [
        path.join(root, "Google", "Chrome", "Application", "chrome.exe"),
        path.join(root, "Microsoft", "Edge", "Application", "msedge.exe"),
      ]),
    );
  }
  if (process.platform === "darwin") {
    return firstExisting([
      "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
      "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
      "/Applications/Chromium.app/Contents/MacOS/Chromium",
    ]);
  }
  return "";
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const url = args.url;
  const outPath = args.out;
  const waitMs = Number(args["wait-ms"] || 15000);
  const resultText = args["result-text"] || "";
  const resultSelector = args["result-selector"] || "";
  const chromePath = findChrome(args.chrome);

  if (!url || !outPath) {
    throw new Error(
      "Usage: --url URL --out FILE [--wait-ms N] " +
        "[--result-selector CSS] [--result-text REGEX] [--chrome PATH]",
    );
  }
  if (!chromePath) {
    throw new Error(
      "Chrome/Chromium not found. Set CHROME_PATH or pass --chrome.",
    );
  }

  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "ejiltalk-headless-"));
  const portFile = path.join(profile, "DevToolsActivePort");
  const child = spawn(
    chromePath,
    [
      "--headless=new",
      "--disable-gpu",
      "--no-first-run",
      "--no-default-browser-check",
      "--disable-extensions",
      "--remote-debugging-port=0",
      `--user-data-dir=${profile}`,
      "--window-size=1440,1000",
      "about:blank",
    ],
    { stdio: "ignore" },
  );

  let ws;
  try {
    await waitForFile(portFile, 20000);
    const [port] = fs.readFileSync(portFile, "utf8").trim().split(/\r?\n/);
    const targets = await fetchJson(`http://127.0.0.1:${port}/json/list`);
    const page = targets.find((item) => item.type === "page");
    if (!page) throw new Error("No page target found");

    ws = new WebSocket(page.webSocketDebuggerUrl);
    let nextId = 0;
    const pending = new Map();
    const send = (method, params = {}) =>
      new Promise((resolve, reject) => {
        const id = ++nextId;
        pending.set(id, { resolve, reject });
        ws.send(JSON.stringify({ id, method, params }));
      });

    ws.addEventListener("message", (event) => {
      const msg = JSON.parse(event.data);
      if (!msg.id || !pending.has(msg.id)) return;
      const item = pending.get(msg.id);
      pending.delete(msg.id);
      if (msg.error) item.reject(new Error(JSON.stringify(msg.error)));
      else item.resolve(msg.result);
    });
    await new Promise((resolve, reject) => {
      ws.addEventListener("open", resolve, { once: true });
      ws.addEventListener("error", reject, { once: true });
    });

    await send("Page.enable");
    await send("Runtime.enable");
    await send("Network.enable");
    await send("Network.setUserAgentOverride", {
      userAgent: DEFAULT_UA,
      acceptLanguage: "en-US,en;q=0.9",
      platform: "Win32",
    });
    await send("Page.addScriptToEvaluateOnNewDocument", {
      source: `Object.defineProperty(navigator, "webdriver", { get: () => false });
               window.chrome = window.chrome || { runtime: {} };`,
    });

    const snapshotExpression = `JSON.stringify({
      href: location.href,
      title: document.title,
      text: (document.body ? document.body.innerText : "").slice(0, 20000),
      hasResultSelector: ${
        resultSelector
          ? `Boolean(document.querySelector(${JSON.stringify(resultSelector)}))`
          : "true"
      }
    })`;
    const evaluateSnapshot = async () => {
      const evaluated = await send("Runtime.evaluate", {
        expression: snapshotExpression,
        returnByValue: true,
        awaitPromise: true,
      });
      return JSON.parse(evaluated.result.value);
    };
    const pollSnapshot = async (attempts, predicate) => {
      let last = null;
      for (let i = 0; i < attempts; i += 1) {
        try {
          last = await evaluateSnapshot();
          if (predicate(last)) return last;
        } catch {
          // Navigation can temporarily destroy the execution context.
        }
        await sleep(1500);
      }
      return last;
    };

    await send("Page.navigate", { url });
    await sleep(waitMs);
    const snapshot = await pollSnapshot(
      30,
      (item) =>
        ((item.title || "").length > 0 || (item.text || "").length > 1500) &&
        (!resultSelector || item.hasResultSelector === true) &&
        (!resultText || new RegExp(resultText, "i").test(item.text || "")),
    );

    const htmlResult = await send("Runtime.evaluate", {
      expression: `document.documentElement ? document.documentElement.outerHTML : ""`,
      returnByValue: true,
      awaitPromise: true,
    });
    const html = htmlResult.result.value || "";
    fs.writeFileSync(outPath, html, "utf8");
    console.log(
      JSON.stringify(
        {
          href: snapshot?.href || "",
          title: snapshot?.title || "",
          html_bytes: Buffer.byteLength(html, "utf8"),
          html_path: outPath,
        },
        null,
        2,
      ),
    );
  } finally {
    try {
      if (ws && ws.readyState === WebSocket.OPEN) ws.close();
    } catch {
      // ignore
    }
    try {
      child.kill();
    } catch {
      // ignore
    }
  }
}

main().catch((error) => {
  console.error(error.stack || error.message);
  process.exitCode = 1;
});
