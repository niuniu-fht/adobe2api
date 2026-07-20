#!/usr/bin/env node

const baseUrl = (process.env.BASE_URL || "http://127.0.0.1:6001").replace(/\/$/, "");
const apiKey = process.env.ADOBE2API_KEY || "";
const prompt = process.argv.slice(2).join(" ") ||
  "A small paper boat glides through a rain puddle, soft daylight, static camera.";

if (!apiKey) {
  throw new Error("Set ADOBE2API_KEY before running this script.");
}

const payload = {
  model: process.env.MODEL || "sd2-fast-4s-16x9-480p",
  content: [{ type: "text", text: prompt }],
  generate_audio: String(process.env.GENERATE_AUDIO || "false").toLowerCase() === "true",
};

if (process.env.SEED) {
  payload.seed = Number(process.env.SEED);
}

const response = await fetch(`${baseUrl}/api/v3/contents/generations/tasks`, {
  method: "POST",
  headers: {
    Authorization: `Bearer ${apiKey}`,
    "Content-Type": "application/json",
  },
  body: JSON.stringify(payload),
});

const body = await response.json();
if (!response.ok) {
  throw new Error(`HTTP ${response.status}: ${JSON.stringify(body)}`);
}

const taskId = body.id;
if (!taskId) {
  throw new Error(`Task response is missing id: ${JSON.stringify(body)}`);
}

const pollIntervalMs = Number(process.env.POLL_INTERVAL_MS || 2000);
const timeoutMs = Number(process.env.TIMEOUT_MS || 900000);
const startedAt = Date.now();

while (Date.now() - startedAt < timeoutMs) {
  await new Promise((resolve) => setTimeout(resolve, pollIntervalMs));
  const taskResponse = await fetch(
    `${baseUrl}/api/v3/contents/generations/tasks/${encodeURIComponent(taskId)}`,
    { headers: { Authorization: `Bearer ${apiKey}` } },
  );
  const task = await taskResponse.json();
  if (!taskResponse.ok) {
    throw new Error(`HTTP ${taskResponse.status}: ${JSON.stringify(task)}`);
  }
  if (task.status === "succeeded") {
    console.log(JSON.stringify(task, null, 2));
    process.exit(0);
  }
  if (["failed", "cancelled", "expired"].includes(task.status)) {
    throw new Error(JSON.stringify(task));
  }
}

throw new Error(`Task ${taskId} timed out after ${timeoutMs}ms`);
