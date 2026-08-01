# Local Service Testing

- The default user-facing launch port remains the one configured by the project launcher (normally `8000`, with its existing fallback behavior). Do not replace it with a temporary test port.
- Use a separate temporary port for local verification when needed.
- After verification succeeds, stop every process started for temporary-port testing and confirm that the port is no longer listening before reporting completion.
- Do not stop the user's normal launcher or service unless the user explicitly requests it.
