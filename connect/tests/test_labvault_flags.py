from django.test import SimpleTestCase

from connect import labvault_flags as flags


class LabvaultFlagsTests(SimpleTestCase):
    def test_customer_sku_flags(self):
        self.assertFalse(flags.AI_NEXUS_ENABLED)
        self.assertFalse(hasattr(flags, "UHD_400G_ENABLED"))
        self.assertFalse(hasattr(flags, "UHD_BFSHELL_ENABLED"))
        self.assertFalse(hasattr(flags, "UHD_CONNECT_REST_ENABLED"))
