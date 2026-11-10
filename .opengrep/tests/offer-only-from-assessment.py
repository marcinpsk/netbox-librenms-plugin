import netbox_librenms_plugin.confirmation_intent
from netbox_librenms_plugin import confirmation_intent
from netbox_librenms_plugin import confirmation_intent as intent
from netbox_librenms_plugin import source_cache, confirmation_intent as tokens
from netbox_librenms_plugin.confirmation_intent import offer, offer as sign_offer, verify


def view(operation, context, submitted, table):
    # ruleid: offer-only-from-assessment
    netbox_librenms_plugin.confirmation_intent.offer(operation, context)
    # ruleid: offer-only-from-assessment
    confirmation_intent.offer(operation, context)
    # ruleid: offer-only-from-assessment
    intent.offer(operation, context)
    # ruleid: offer-only-from-assessment
    tokens.offer(operation, context)
    # ruleid: offer-only-from-assessment
    offer(operation, context)
    # ruleid: offer-only-from-assessment
    sign_offer(operation, context)
    # ok: offer-only-from-assessment
    intent.verify(submitted, policy=None, context=context, rebuild=None)
    # ok: offer-only-from-assessment
    verify(submitted, policy=None, context=context, rebuild=None)
    # ok: offer-only-from-assessment
    table.offer(operation)
    return source_cache
