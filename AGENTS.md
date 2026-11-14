# Verification

Use the matching project console pytest entrypoint, as CI does. Do not substitute `python -m pytest`: it adds the checkout root to `sys.path` and can hide import collection failures. Keep the required test targets unchanged.
