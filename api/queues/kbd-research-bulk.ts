// Broad non-exact leaf work has its own topic and consumer group so it cannot
// consume the exact leaf queue's concurrency budget. The callback boundary is
// intentionally identical across both leaf queues.
export {
  default,
  POST,
} from "../../serverless/kbd-research-queue-callback.js";
