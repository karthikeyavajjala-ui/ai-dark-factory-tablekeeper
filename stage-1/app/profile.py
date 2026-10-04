"""Which stage of the service this folder is.

Every stage folder ships the same domain engine. What separates them is the profile:
which surfaces are routed, which fields a request may carry, and which response fields
appear. Keeping the difference in one small file makes the folders diffable against
each other, and makes "this folder answers this stage and not the next one" a property
of the profile rather than of a hand-edited copy.
"""
STAGE = 1

# Derived capabilities. Named here so the engine reads as intent, not as arithmetic.
COMBINATIONS = STAGE >= 2     # declared pairs, available_options, the browser product
UI = STAGE >= 2               # the four browser routes
POLICIES = STAGE >= 3         # dated policies, accepted terms, explain, revisions
HISTORY = STAGE >= 3          # reservation history and history-reading endpoints
SERIES = STAGE >= 3           # recurring agreements
REPLAN = STAGE >= 4           # closure preview and application, series amendment
