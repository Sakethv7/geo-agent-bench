# Findings

Seven findings from the benchmark, each with its evidence and its limit. The numbers come from the tables in `reports/`. The design docs explain how each was measured.

**1. A shortcut in the page head is invisible to a fetch tool.** Claude's fetch tool returns the page as markdown text and drops the HTML head. A `<link rel="alternate">` to a markdown copy never reached the model. Only a visible pointer line in the page body worked, and even then Claude read `llms.txt` in 1 of 6 runs on a 9-page site. Limit: one agent tool, one small site.

**2. A fetch tool can cache for 21 minutes or more, by full URL.** Repeat runs on one host were served from the tool's cache and left no trace in our log. The fix was one fresh host name per run, so each run has its own cache key and its own token. Limit: measured for Claude's tool, and suspected once for OpenAI's.

**3. Use absolute links in markdown that you serve to agents.** A fetch tool follows only URLs it has seen. So it cannot use a relative link in raw markdown. My first `geo-l` version used relative `.md` links and failed 0 of 8. After I made the links absolute, it answered 8 of 8. Limit: this is the behavior of one tool. A human reader would never notice it.

**4. Serving markdown at the same URL to a verified agent cut tokens by about two thirds.** Take the 6 tasks that all three variants solved. Mean input tokens were 101k for plain HTML and 86k with `llms.txt` and markdown copies. They were 33k with the interceptor. I predicted the tokens would barely change, and I was wrong. My explanation is that the tool keeps menus and footers when it converts HTML, but I did not test it. Limit: one run per cell, one agent.

**5. A valid sign-in does not prove who is asking.** A spoofer that copied Claude's name and held a valid OAuth token read the protected page. Signed requests (Web Bot Auth) separated signed from spoofed agents. Copying a user agent name is common: one address sent 397 secret-file probes under Claude's name. Limit: my own signer and verifier, checked against Cloudflare's independent one.

**6. A small local model needed a better harness, not a better model.** Qwen3-8B scored 0 of 12 with plain fetch tools. It scored 12 of 12 with deterministic navigation and a bounded context. The same model failed the OAuth task 0 of 3 because it never followed the link in the 401 reply. Limit: one model, one quantization.

**7. OpenAI's hosted web search fetches live and names itself.** A probe showed `ChatGPT-User` and `OAI-SearchBot` requests from IPs inside the files OpenAI publishes. Three fetches came from three IPs, so matching runs by IP would fail and matching by host token works. A repeat page was served from a cache. Limit: one probe, two time windows, one model.
