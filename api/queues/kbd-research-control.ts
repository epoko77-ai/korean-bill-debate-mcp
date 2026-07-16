// Coordinator and barrier messages use a dedicated topic, but deliberately
// share the exact callback implementation and Python dispatch boundary with
// leaf work. This keeps deployment pinning, retries, acknowledgements, and
// terminal-failure handling identical across both queues.
export {
  default,
  POST,
} from "../../serverless/kbd-research-queue-callback.ts";
