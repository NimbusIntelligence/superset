# Basic imports
import logging
import os
import requests

# Imports for landing page
import urllib.parse
from markupsafe import Markup
from flask import request, render_template_string
from flask_appbuilder import expose, IndexView

# Imports for Snowflake authentication
from flask_appbuilder.security.manager import AUTH_REMOTE_USER
from superset.utils.core import QuerySource

# Imports for cache configuration
from cachelib.file import FileSystemCache
from celery.schedules import crontab
from datetime import timedelta


# Add logger for output to Snowflake logs
logger = logging.getLogger("superset-service")

# Get environment variables set during container initialization
# Get Snowflake account
ACCOUNTNAME = os.getenv("ACCOUNTNAME")
# Get hostname of metadata SQL container (difference between Docker and SFCS)
SQLHOST = os.getenv("SQLHOST")
# Get hostname of OAuth container (difference between Docker and SFCS)
AUTHHOST = os.getenv("AUTHHOST")
# Get the SQL password
SQLPASS = os.getenv("PASSWORD")

# Superset won't initialize without a key set, overwrite in prod
SECRET_KEY = "YOUR_VERY_OWN_RANDOM_GENERATED_STRING"

# Makes sure Snowflake is offered when adding a database connection
PREFERRED_DATABASES = ["Snowflake"]

# Deal with SFCS header shenanigans
ENABLE_CORS = True
ENABLE_PROXY_FIX = True

# Enable creating new Superset users from Snowflake credentials
AUTH_USER_REGISTRATION = True
AUTH_USER_REGISTRATION_ROLE = "Admin"


# Retrieve Snowflake authenticated username from web request header
# See Superset/Flask documentation for Middleware explanation
class RemoteUserMiddleware(object):
    def __init__(self, app):
        self.app = app

    def __call__(self, environ, start_response):
        user = environ.get("HTTP_SF_CONTEXT_CURRENT_USER", None)
        environ["REMOTE_USER"] = user
        return self.app(environ, start_response)


# Use Snowflake authenticated username as Superset authentication
ADDITIONAL_MIDDLEWARE = [
    RemoteUserMiddleware,
]
AUTH_TYPE = AUTH_REMOTE_USER

# Set metadatabase
SQLALCHEMY_DATABASE_URI = f"postgresql://user:{SQLPASS}@{SQLHOST}:5432/nimbus"

# Configure cache
# Currently a mounted block storage, switch to Redis in future?
BASE_CACHE_DIR = "/app/superset-cache/"
CACHE_CONFIG = {
    "CACHE_TYPE": "FileSystemCache",
    "CACHE_DEFAULT_TIMEOUT": 60 * 60 * 24,
    "CACHE_THRESHOLD": 100,
    "CACHE_DIR": BASE_CACHE_DIR,
}
RESULTS_BACKEND = FileSystemCache(
    cache_dir=os.path.join(BASE_CACHE_DIR, "results/"),
    threshold=100,
    default_timeout=60 * 60 * 24,
)
THUMBNAIL_CACHE_CONFIG = {
    "CACHE_TYPE": "FileSystemCache",
    "CACHE_DEFAULT_TIMEOUT": 60 * 60 * 24,
    "CACHE_KEY_PREFIX": "thumbnail_",
    "CACHE_DIR": BASE_CACHE_DIR + "thumbnails/",
}

# Celery config for worker instances (async executions)
# Currently on SQL backend, switch to Redis in future?
CELERY_BEAT_SCHEDULER_EXPIRES = timedelta(weeks=1)


class CeleryConfig:  # pylint: disable=too-few-public-methods
    broker_url = f"sqla+postgresql://user:{SQLPASS}@{SQLHOST}:5432/nimbus"
    imports = ("superset.sql_lab", "superset.tasks.scheduler")
    result_backend = f"db+postgresql://user:{SQLPASS}@{SQLHOST}:5432/nimbus"
    CELERYD_LOG_LEVEL = "DEBUG"
    worker_prefetch_multiplier = 1
    task_acks_late = False
    task_annotations = {
        "sql_lab.get_sql_results": {
            "rate_limit": "100/s",
        },
    }
    beat_schedule = {
        "reports.scheduler": {
            "task": "reports.scheduler",
            "schedule": crontab(minute="*", hour="*"),
            "options": {"expires": int(CELERY_BEAT_SCHEDULER_EXPIRES.total_seconds())},
        },
        "reports.prune_log": {
            "task": "reports.prune_log",
            "schedule": crontab(minute=0, hour=0),
        },
    }


CELERY_CONFIG: type[CeleryConfig] = CeleryConfig


# Configure connection mutator (defer Snowflake connections to OAuth)
class TokenError(Exception):
    pass


def DB_CONNECTION_MUTATOR(uri, params, username, security_manager, source):
    if username and source is QuerySource.SQL_LAB:
        user = security_manager.find_user(username=username)
    else:
        user = security_manager.current_user

    if uri.drivername == "snowflake" and uri.query["authenticator"] == "oauth":
        logger.critical("Flask username: " + user.username)
        uri = uri.set(username=user.username)
        uri = uri.set(password="")
        logger.critical("Username after set: " + uri.username)
        uri = uri.set(host=ACCOUNTNAME)
        logger.critical("Account after set: " + uri.host)
        result = requests.get(
            f"http://{AUTHHOST}:8080/superset_token?account={uri.host}&user={uri.username}"
        )
        token_json = result.json()
        logger.critical(token_json)
        if token_json["success"] == True:
            uri = uri.update_query_dict({"token": token_json["token"]})
            logger.info(uri)
            return uri, params
        else:
            raise TokenError(token_json["error"])
    else:
        return uri, params


# Configure custom landing page to initialize oauth dance
class SupersetDashboardIndexView(IndexView):
    @expose("/")
    def index(self):
        SUPERSET_URI = request.base_url
        grant_url_result = requests.get(
            f"http://{AUTHHOST}:8080/grant_url?account={ACCOUNTNAME}&superset={urllib.parse.quote(SUPERSET_URI)}"
        )
        grant_url_json = grant_url_result.json()
        if grant_url_json["success"] == True:
            grant_url = f"""<li><a href="{grant_url_json['url']}">Snowflake OAuth as default role</a></li>
        <li><a href="{grant_url_json['url']}&scope=session%3Arole%3ASYSADMIN">Snowflake OAuth as sysadmin</a></li>"""
        else:
            grant_url = f"""<li>Error: {grant_url_json['error']}</li>
        <li>Contact your administrator to initialize the OAuth integration</li>"""
        html = """
    <!DOCTYPE html>
    <html>
    <head>
        <link rel="icon" type="image/png" href="/static/assets/images/favicon.png">
        <style>
            body {
                font-family: Inter,Helvetica,Arial;
                -webkit-font-smoothing: antialiased;
                color: #333;
                font-size: 14px;
                line-height: 1.4;
                box-sizing: border-box;
            }
            .navbar {
                display: flex;
                align-items: center;
                background-color: #fff;
                border-bottom: 2px solid #F7F7F7;
                padding-left: 1rem;
            }
            .navbar-brand {
                display: inline-block;
                padding-top: .3125rem;
                padding-bottom: .3125rem;
                margin-right: 1rem;
                font-size: 1.25rem;
                line-height: inherit;
                white-space: nowrap;
            }
            .navbar-brand img {
                cursor: pointer;
                font-size: 18px;
                line-height: 19px;
                border: 0;
                box-sizing: border-box;
                display: block;
                height: 30px;
                object-fit: contain;
            }
            .main-nav {
                list-style-type: none;
                margin: 0;
                padding: 0;
                overflow: hidden;
                display: flex;
                align-items: center;
            }
            .main-nav li a {
                display: block;
                color: #333;
                text-align: center;
                padding: 14px 16px;
                text-decoration: none;
                border-bottom: 2px solid transparent;
            }
            .main-nav li a {
                display: block;
                color: #484848;
                text-align: center;
                padding: 14px 16px;
                text-decoration: none;
            }
            .info-section {
                color: darkgreen;
                width: 100%;
                padding: 20px;
                margin-top: 20px;
                background-color: #f9f9f9;
                font-size: 15px;
                line-height: 1.6;
            }
            .info-section ul {
                padding-left: 30px;
                list-style-position: inside;
            }
            .info-section b {
                font-size: 18px;
            }
            .main-nav li a:hover {
              background-color: #f2f2f2;
              border-bottom: 2px solid #1EA7C8;
            }
        </style>
    </head>
    <body>
        <div class="navbar">
            <a class="navbar-brand" href="superset/welcome/">
                <img src="/static/assets/images/superset-logo-horiz.png" alt="Superset">
            </a>
            <ul class="main-nav">
                <li><a href="dashboard/list/">Dashboards</a></li>
                <li><a href="chart/list/">Charts</a></li>
                <li><a href="tablemodelview/list/">Datasets</a></li>
                <li><a href="sqllab/">SQL</a></li>
            </ul>
        </div>
        <div class="info-section">
            <ul>
                <b>Nimbus OAuth Integration</b>
                <li>You are authenticated to Superset as {{sf_user}}</li>
                <li>Your authentication to {{account_name}} is valid</li>
                <b>Update authentication</b>
                {{grant_url}}                     
                <b>Continue to Superset</b>
                <li><a href="dashboard/list/">Superset: Dashboards</a></li>
            </ul>
        </div>
    </body>
    </html>
    """
        return render_template_string(
            html,
            grant_url=Markup(grant_url),
            account_name=ACCOUNTNAME,
            sf_user=request.environ["REMOTE_USER"],
        )


FAB_INDEX_VIEW = (
    f"{SupersetDashboardIndexView.__module__}.{SupersetDashboardIndexView.__name__}"
)
