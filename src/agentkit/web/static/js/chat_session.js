(() => {
  function createChatSessionGuard() {
    let sequence = 0;
    let active = null;

    return {
      begin(conversationId) {
        active?.controller.abort();
        const controller = new AbortController();
        active = {
          sequence: ++sequence,
          conversationId: String(conversationId || ""),
          controller,
        };
        return Object.freeze({
          sequence: active.sequence,
          conversationId: active.conversationId,
          signal: controller.signal,
        });
      },

      isCurrent(token) {
        return Boolean(
          active &&
          token &&
          active.sequence === token.sequence &&
          active.conversationId === token.conversationId &&
          !token.signal.aborted
        );
      },

      cancel() {
        active?.controller.abort();
        active = null;
      },
    };
  }

  window.AgentKitChatSession = Object.freeze({ createChatSessionGuard });
})();
