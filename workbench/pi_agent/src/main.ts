import { createPiServer } from "./server.js";
import { PiAgentProvider } from "./provider.js";

const host = process.env.PI_AGENT_HOST ?? "127.0.0.1";
const port = Number(process.env.PI_AGENT_PORT ?? 0);
const token = process.env.PI_AGENT_TOKEN;
if (!token) throw new Error("PI_AGENT_TOKEN is required");
const app = createPiServer({ token, provider: new PiAgentProvider() });
const address = await app.listen(port, host);
process.stdout.write(JSON.stringify({ ...address, protocol_version: "1" }) + "\n");
process.on("SIGTERM", () => { void app.close().finally(() => process.exit(0)); });

// 父进程被强杀时不会发 SIGTERM，这个子进程会变成孤儿并一直占着固定端口，
// 让下一次启动必然失败。自己盯着父进程，它没了就退出。
const parentPid = Number(process.env.PI_AGENT_PARENT_PID ?? 0);
if (parentPid > 0) {
  setInterval(() => {
    try {
      process.kill(parentPid, 0);
    } catch {
      void app.close().finally(() => process.exit(0));
    }
  }, 2000).unref();
}
