module.exports = {
  apps: [{
    name: 'whatsapp-bot',
    script: 'backend/main.py',
    interpreter: '/home/richird/whatsapp-bot/.venv/bin/python3',
    cwd: '/home/richird/whatsapp-bot',
    instances: 1,
    autorestart: true,
    watch: false,
    max_memory_restart: '1G',
    env: {
      NODE_ENV: 'production',
      PYTHONUNBUFFERED: '1'
    },
    log_file: '/home/richird/whatsapp-bot/logs/combined.log',
    out_file: '/home/richird/whatsapp-bot/logs/out.log',
    error_file: '/home/richird/whatsapp-bot/logs/error.log',
    log_date_format: 'YYYY-MM-DD HH:mm:ss Z',
    merge_logs: true
  }]
};
