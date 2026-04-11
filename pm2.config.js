const path = require("path");

module.exports = {
  apps: [
    {
      name: "voicecord",
      cwd: __dirname,
      script: "main.py",
      interpreter: path.join(__dirname, ".venv", "bin", "python"),
      autorestart: true,
      restart_delay: 5000,
      max_restarts: 20,
      watch: false,
      env: {
        PYTHONUNBUFFERED: "1",
      },
    },
  ],
};
