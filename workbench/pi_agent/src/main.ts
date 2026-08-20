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
