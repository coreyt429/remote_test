systemctl --user restart celery.service
systemctl --user restart api.service
systemctl --user status --no-pager
