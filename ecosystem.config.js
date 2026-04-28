const path = require("path");

module.exports = {
  apps: [
    {
      name: "voicecord",
      cwd: __dirname,
      script: "main.py",
      interpreter: path.join(__dirname, ".venv", "bin", "python"),
      autorestart: true,
      // Backoff between restarts grows up to ~15s when the process keeps
      // crashing; resets after `min_uptime` of stable runtime.
      restart_delay: 5000,
      exp_backoff_restart_delay: 5000,
      min_uptime: "60s",
      // High cap so PM2 keeps trying after long-running crashes weeks later.
      // (`min_uptime` makes this safe; rapid crash loops still stop quickly.)
      max_restarts: 1000,
      kill_timeout: 5000,
      watch: false,
      log_date_format: "YYYY-MM-DD HH:mm:ss Z",
      env: {
        PYTHONUNBUFFERED: "1",
      },
    },
  ],
};
