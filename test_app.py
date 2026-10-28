import json
import re
import subprocess

import server


def test_feed_renders_user_content_as_text(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "DB", str(tmp_path / "social.db"))
    server.init()
    client = server.app.test_client()
    payload = '<img src=x onerror=alert(1)>'
    profile = {
        "name": payload,
        "role": "role " + payload,
        "skills": "skills " + payload,
        "task": "task " + payload,
    }
    assert client.post("/api/profiles", json=profile).status_code == 200
    posts = [
        {"author": "author " + payload, "topic": "topic " + payload,
         "kind": "kind " + payload, "body": "body & " + payload, "reply_to": payload},
        {"author": "a", "topic": "t", "kind": "verdict", "body": "verdict body"},
        {"author": "b", "topic": "t", "kind": "challenge", "body": "challenge body"},
        {"author": "c", "topic": "t", "kind": "challenge reply extra", "body": "other body"},
    ]
    for post in posts:
        assert client.post("/api/posts", json=post).status_code == 200

    html = client.get("/").get_data(as_text=True)
    script = re.search(r"<script>(.*?)</script>", html, re.DOTALL).group(1)
    data = {
        "script": script,
        "profiles": client.get("/api/profiles").get_json(),
        "posts": client.get("/api/posts").get_json(),
        "stats": client.get("/api/stats").get_json(),
        "payload": payload,
    }
    # Run the actual page script with a small DOM, so untrusted HTML sinks and
    # the resulting text and CSS classes are checked together.
    harness = r"""
const assert = require('assert');
const vm = require('vm');
const data = JSON.parse(require('fs').readFileSync(0, 'utf8'));
class Element {
  constructor(tag) { this.tagName = tag; this.className = ''; this.children = []; this.text = ''; }
  set textContent(value) { this.text = value == null ? '' : String(value); this.children = []; }
  get textContent() { return this.text + this.children.map(c => c.textContent).join(''); }
  set innerHTML(value) {
    assert(!value.includes('<img'), 'untrusted markup reached innerHTML');
    this.text = ''; this.children = [];
  }
  append(...children) {
    this.children.push(...children.map(c => {
      if (typeof c !== 'string') return c;
      const text = new Element('#text'); text.textContent = c; return text;
    }));
  }
  replaceChildren(...children) { this.text = ''; this.children = []; this.append(...children); }
  get classList() { return {add: c => { this.className += (this.className ? ' ' : '') + c; }}; }
}
global.document = {createElement: tag => new Element(tag)};
global.profiles = new Element('div');
global.feed = new Element('div');
global.stats = new Element('span');
global.fetch = async url => ({json: async () => data[url.split('/').pop()]});
global.setInterval = () => {};
(async () => {
  await vm.runInThisContext(data.script + '\nload()');
  const profile = data.profiles[0];
  assert.strictEqual(profiles.children.length, 1);
  assert.strictEqual(profiles.children[0].textContent,
    `${profile.name} ${profile.role}${profile.skills}\n${profile.task}`);
  assert.strictEqual(feed.children.length, data.posts.length);
  for (const [index, post] of data.posts.slice().reverse().entries()) {
    const card = feed.children[index];
    const classes = ['card'];
    if (post.reply_to) classes.push('reply');
    if (['verdict', 'challenge'].includes(post.kind)) classes.push(post.kind);
    assert.deepStrictEqual(card.className.split(/\s+/).sort(), classes.sort());
    const meta = `${post.kind} · ${post.topic}${post.reply_to ? ' · reply to #' + post.reply_to : ''} · #${post.id}`;
    assert.strictEqual(card.textContent, `${post.author} ${meta}${post.body}`);
  }
  assert(stats.textContent.includes(data.payload));
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    result = subprocess.run(["node", "-e", harness], input=json.dumps(data),
                            text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
