# ruleid: signing-only-in-confirmation-intent
from django.core import signing

# ruleid: signing-only-in-confirmation-intent
from django.core import cache, signing as token_signing

# ruleid: signing-only-in-confirmation-intent
from django.core.signing import dumps

# ruleid: signing-only-in-confirmation-intent
from django.core.signing import loads as load_token

# ruleid: signing-only-in-confirmation-intent
import django.core.signing

# ruleid: signing-only-in-confirmation-intent
import django.core.signing as django_signing

# ok: signing-only-in-confirmation-intent
from django.core import validators

# ok: signing-only-in-confirmation-intent
from netbox_librenms_plugin import confirmation_intent

# ok: signing-only-in-confirmation-intent
import hashlib


def token(value, salt):
    return signing.dumps(value, salt=salt), token_signing, dumps, load_token, django_signing, cache, validators
