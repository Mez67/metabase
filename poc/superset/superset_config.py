# Superset settings for the proof of concept.
import os

SECRET_KEY = os.environ["SUPERSET_SECRET_KEY"]
SQLALCHEMY_DATABASE_URI = "sqlite:////app/superset_home/superset.db?check_same_thread=false"

FEATURE_FLAGS = {
    "EMBEDDED_SUPERSET": True,
}

# Guest tokens (embedded dashboards) get this role. The setup script gives it
# read access to the sales dataset only.
GUEST_ROLE_NAME = "EmbedViewer"
GUEST_TOKEN_JWT_EXP_SECONDS = 600

# Let the portal show Superset pages in an iframe. In production keep Talisman
# on and allow only your portal: frame-ancestors 'self' https://portal.corp.local
TALISMAN_ENABLED = False
HTTP_HEADERS = {}
SESSION_COOKIE_SAMESITE = "None"
SESSION_COOKIE_SECURE = False  # set True behind HTTPS
WTF_CSRF_ENABLED = True

# --- sign-in: Active Directory / LDAP ---------------------------------------
# SUPERSET_AUTH=ldap signs users in against the directory; "db" keeps local
# accounts (used for a quick test without LDAP).
if os.environ.get("SUPERSET_AUTH", "ldap") == "ldap":
    from flask_appbuilder.security.manager import AUTH_LDAP

    AUTH_TYPE = AUTH_LDAP
    AUTH_LDAP_SERVER = os.environ.get("LDAP_URL", "ldap://ldap:389")
    AUTH_LDAP_USE_TLS = False
    AUTH_LDAP_BIND_USER = os.environ.get("LDAP_BIND_DN", "cn=readonly,dc=corp,dc=local")
    AUTH_LDAP_BIND_PASSWORD = os.environ.get("LDAP_BIND_PASSWORD", "readonly")
    AUTH_LDAP_SEARCH = os.environ.get("LDAP_USER_BASE", "ou=users,dc=corp,dc=local")
    # Active Directory: "sAMAccountName"
    AUTH_LDAP_UID_FIELD = os.environ.get("LDAP_UID_FIELD", "uid")
    AUTH_LDAP_FIRSTNAME_FIELD = "givenName"
    AUTH_LDAP_LASTNAME_FIELD = "sn"
    AUTH_LDAP_EMAIL_FIELD = "mail"
    AUTH_LDAP_GROUP_FIELD = "memberOf"
    AUTH_USER_REGISTRATION = True
    AUTH_USER_REGISTRATION_ROLE = "Gamma"
    AUTH_ROLES_SYNC_AT_LOGIN = True
    AUTH_ROLES_MAPPING = {
        os.environ.get("LDAP_ANALYST_GROUP", "cn=bi-analysts,ou=groups,dc=corp,dc=local"): ["Admin"],
    }
