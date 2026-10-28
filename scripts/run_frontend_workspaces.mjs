import { spawn } from "node:child_process";
import { glob, readFile } from "node:fs/promises";
import { availableParallelism } from "node:os";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const repositoryRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const scriptName = process.argv[2];

if (!scriptName) {
  console.error("Usage: node scripts/run_frontend_workspaces.mjs <script>");
  process.exit(2);
}

function parseJobCount() {
  const configured = process.env.CARLINA_FRONTEND_JOBS;
  if (configured === undefined || configured === "") {
    return Math.max(1, Math.min(2, availableParallelism()));
  }

  const parsed = Number(configured);
  if (!Number.isSafeInteger(parsed) || parsed < 1) {
    console.error("CARLINA_FRONTEND_JOBS must be a positive integer.");
    process.exit(2);
  }
  return parsed;
}

async function discoverWorkspaces() {
  const rootPackage = JSON.parse(
    await readFile(resolve(repositoryRoot, "package.json"), "utf8"),
  );
  const patterns = Array.isArray(rootPackage.workspaces)
    ? rootPackage.workspaces
    : rootPackage.workspaces?.packages ?? [];
  const packageFiles = new Set();

  for (const pattern of patterns) {
    for await (const packageFile of glob(`${pattern}/package.json`, {
      cwd: repositoryRoot,
    })) {
      packageFiles.add(packageFile);
    }
  }

  const workspaces = [];
  for (const packageFile of [...packageFiles].sort()) {
    const packageJson = JSON.parse(
      await readFile(resolve(repositoryRoot, packageFile), "utf8"),
    );
    if (packageJson.scripts?.[scriptName]) {
      workspaces.push({
        location: dirname(packageFile).replaceAll("\\", "/"),
        name: packageJson.name,
      });
    }
  }
  return workspaces;
}

function runWorkspace(workspace) {
  return new Promise((complete) => {
    const npmExecPath = process.env.npm_execpath;
    const executable = npmExecPath ? process.execPath : "npm";
    const arguments_ = npmExecPath
      ? [npmExecPath, "run", scriptName, "--workspace", workspace.name]
      : ["run", scriptName, "--workspace", workspace.name];
    const child = spawn(
      executable,
      arguments_,
      {
        cwd: repositoryRoot,
        env: process.env,
        shell: false,
        stdio: ["ignore", "pipe", "pipe"],
      },
    );
    const stdout = [];
    const stderr = [];
    child.stdout.on("data", (chunk) => stdout.push(chunk));
    child.stderr.on("data", (chunk) => stderr.push(chunk));
    child.once("error", (error) => {
      complete({ code: 1, error, stderr, stdout, workspace });
    });
    child.once("close", (code, signal) => {
      complete({ code: code ?? 1, signal, stderr, stdout, workspace });
    });
  });
}

function printResult(result) {
  const status = result.code === 0 ? "PASS" : "FAIL";
  console.log(`\n[${status}] ${result.workspace.name} (${result.workspace.location})`);
  for (const chunk of result.stdout) process.stdout.write(chunk);
  for (const chunk of result.stderr) process.stderr.write(chunk);
  if (result.error) console.error(result.error);
  if (result.signal) console.error(`Process terminated by ${result.signal}.`);
}

const workspaces = await discoverWorkspaces();
if (workspaces.length === 0) {
  console.log(`No workspace defines the ${JSON.stringify(scriptName)} script.`);
  process.exit(0);
}

const jobCount = Math.min(parseJobCount(), workspaces.length);
console.log(
  `Running ${JSON.stringify(scriptName)} in ${workspaces.length} workspaces `
    + `with ${jobCount} concurrent job${jobCount === 1 ? "" : "s"}.`,
);

const results = new Array(workspaces.length);
let nextIndex = 0;
async function worker() {
  while (nextIndex < workspaces.length) {
    const index = nextIndex;
    nextIndex += 1;
    const workspace = workspaces[index];
    console.log(`[START] ${workspace.name}`);
    const result = await runWorkspace(workspace);
    results[index] = result;
    printResult(result);
  }
}

await Promise.all(Array.from({ length: jobCount }, () => worker()));
const failed = results.filter((result) => result.code !== 0);
if (failed.length > 0) {
  console.error(
    `\n${failed.length} workspace${failed.length === 1 ? "" : "s"} failed: `
      + failed.map((result) => result.workspace.name).join(", "),
  );
  process.exit(1);
}

console.log(`\nAll ${workspaces.length} workspaces passed ${JSON.stringify(scriptName)}.`);
