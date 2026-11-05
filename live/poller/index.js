import { run } from './poller-core.js'

export default {
  async scheduled(event, env) {
    await run(env)
  }
}
