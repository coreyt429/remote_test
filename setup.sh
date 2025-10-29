# become the ubuntu user if you aren’t already
whoami

# create a venv for the API
python3 -m venv /home/ubuntu/api/.venv
source /home/ubuntu/api/.venv/bin/activate

# install runtime deps
pip install --upgrade pip
pip install gunicorn flask
pip install celery
