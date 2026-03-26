# Rufus Generator

Amazon Rufus/COSMOS listing generator with vision AI. FastAPI + Celery + PostgreSQL + Redis.

## Deploy Configuration (configured by /setup-deploy)
- Platform: Heroku
- App name: rufus-generator
- Production URL: https://rufus-generator-a2dd749774e5.herokuapp.com/
- Deploy workflow: `git push heroku elchunenfreund/heroku-deploy:main` (or merge to main + auto-deploy)
- Deploy status command: `heroku ps -a rufus-generator`
- Merge method: squash
- Project type: web app (FastAPI + Celery worker)
- Post-deploy health check: https://rufus-generator-a2dd749774e5.herokuapp.com/health

### Custom deploy hooks
- Pre-merge: none
- Deploy trigger: `git push heroku HEAD:main`
- Deploy status: `heroku ps -a rufus-generator`
- Health check: `curl -sf https://rufus-generator-a2dd749774e5.herokuapp.com/health`
- Migrations: `heroku run "alembic upgrade head" -a rufus-generator` (add `release` Procfile entry to automate)

### Dynos
- web: Basic ($7/mo) — uvicorn
- worker: needs scaling — `heroku ps:scale worker=1 -a rufus-generator`

### Add-ons
- heroku-postgresql: essential-0 (~$5/mo)
- heroku-redis: mini (~$3/mo)

### Known limitations
- Ephemeral filesystem: uploads lost on dyno restart (need S3 for persistent storage)
- WeasyPrint PDF generation requires system libs (pango/cairo) not available on Heroku Python buildpack
