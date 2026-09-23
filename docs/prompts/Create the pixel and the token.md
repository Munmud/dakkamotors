You're helping me set up Meta advertising for dakkamotors.com, a used car
dealership in Hamura, Tokyo. I'm already logged into my Meta Business account.
Work through this and stop at each STOP.

Part A — the pixel
1. Go to Meta Events Manager (business.facebook.com/events_manager).
2. Connect a new data source: Web → Meta Pixel. Name it exactly "Dakka Motors".
3. When it offers installation options, SKIP them — the site already has the
   snippet. I only need the number.
4. Find the Pixel ID (a long number) and tell it to me in your reply.

Part B — the system user that lets an ad pause itself
5. Go to Business Settings → Users → System users. Add one named
   "dakkamotors-site" with the role Employee.
6. On that system user, click "Add assets" → Ad accounts → select my ad account
   → turn ON "Manage campaigns". Confirm it saved.
7. Click "Generate new token". Pick my app. Tick the "ads_management"
   permission. Set the token expiry to "Never" (not 60 days).
8. STOP before clicking Generate. Tell me you're ready, and let ME click it and
   copy the token myself. Do not read the token aloud, do not type it into this
   chat, and do not paste it anywhere. It's a credential.

Report back with: the Pixel ID, and confirmation that the system user has
"Manage campaigns" on the ad account. If any screen looks different from the
above, describe what you see instead of guessing.
