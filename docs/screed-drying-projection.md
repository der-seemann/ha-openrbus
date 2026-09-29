# Screed-drying entity projection

Screed drying (Estrichtrocknung / Estrich-Aufheizprogramm) is disabled by
default through `screed_drying_enabled` in both the initial access-level flow
and the Options flow.  This is an entity-projection policy: it never sends a
bus write.

While disabled, the integration excludes screed rows before platform setup and
before polling groups are assembled.  Enabling the option and reloading the
entry creates the normal sensor/control projections from the Core catalogue.
Disabling it later integration-disables any already registered screed entities;
their unique IDs and registry rows remain intact.  An explicit user disable is
never replaced when the option is toggled.

The scope is deliberately narrow and derived from the manufacturer/Core
catalogue: canonical `Screed*` codes or English/German `screed` / `Estrich`
labels.  The current canonical rows include `344d:00`–`344f:00`,
`3483:00`–`348c:00`, and `5447:00`–`544a:00`.  Generic heating programs,
timers and temperatures are not classified as screed drying.
