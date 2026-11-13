import mycel.trust as trust

# Real nodes mint 22-bit tickets (~1 s). Tests use cheap ones so suites stay fast.
trust.GLOBAL_BITS = 8
