import os

DEBUG = True

BASE_DIR = os.path.abspath(os.path.dirname(__file__))

SQLALCHEMY_DATABASE_URI = ""
DATABASE_CONNECT_OPTIONS = {}

THREADS_PER_PAGE = 2

CSRF_ENABLED = True

SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-me")

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")