# FAQ

**Do I ever have to copy a token?**
No. You log in with your name and password, and the login page (or the invite link) gives the token to the extension.
A token is only for a client without OAuth, or when you have no password yet.

**Does Claude ever see my password?**
No. You type it on a page of your own server, opened through a one-time link. The server logs in, stores the session
and the password encrypted, and only sends them to that site.

**Can other users see my sessions?**
No. Tools are shared, sessions are not. Each session is encrypted and bound to its user and site; a tool always runs
with the session of the user who calls it.

**How do I log Claude out?**
Remove the connector in Claude. If Claude revokes its token, that login ends on the server at once. To end every login
at once, the admin runs `python -m connector.users add <name>` for your name. You get a new token, and every Claude
login and the remembered browser login stop working.

**How many LLM calls does it take?**
One per recording, to choose and name the tools. Calling a tool uses no model.

**Which MCP clients work?**
claude.ai and Claude Code, through OAuth, are tested. Claude Desktop uses the same OAuth flow but has not been tried
yet. Any other client that speaks streamable HTTP works too, with OAuth or with a bearer token. The server speaks both
the current and the 2026-07-28 MCP protocol.

**Is this a scraper?**
It replays the requests a site's own frontend made, on your own account, one call at a time, after its terms were
read. It does not crawl, and it stops when a site blocks it.

**What happens when a site changes?**
The tool fails with an error instead of returning an empty answer, because a missing response path is an error. For
now, repair is manual: remove the tool from the registry and record the action again. A new recording does not yet
replace a tool with the same name.
