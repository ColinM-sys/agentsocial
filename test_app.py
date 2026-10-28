import json
import shutil
import subprocess

import pytest

import server


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is needed to exercise the feed JavaScript")
def test_feed_renders_stored_values_as_text(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "DB", str(tmp_path / "social.db"))
    server.init()
    client = server.app.test_client()

    profile = {
        "name": '<img src=x onerror=alert("name")>',
        "role": '<svg onload=alert("role")>',
        "skills": '<img src=x onerror=alert("skills")> &lt;',
        "task": '<img src=x onerror=alert("task")>',
    }
    posts = [
        {
            "author": '<img src=x onerror=alert("author")>',
            "kind": 'verdict reply"><img src=x onerror=alert("kind")>',
            "topic": '<img src=x onerror=alert("topic")>',
            "body": '<img src=x onerror=alert("body")> &lt;',
            "reply_to": '<img src=x onerror=alert("reply")>',
        },
        {"author": "verdict author", "kind": "verdict", "body": "verdict", "reply_to": 1},
        {"author": "challenge author", "kind": "challenge", "body": "challenge"},
        {"author": "unknown author", "kind": "reply verdict", "body": "unknown"},
    ]
    assert client.post("/api/profiles", json=profile).status_code == 200
    for post in posts:
        assert client.post("/api/posts", json=post).status_code == 200

    page = client.get("/").get_data(as_text=True)
    script = page.split("<script>", 1)[1].split("</script>", 1)[0]
    data = {
        "script": script,
        "responses": {path: client.get(path).get_json() for path in
                      ("/api/profiles", "/api/posts", "/api/stats")},
        "profile": profile,
        "posts": posts,
    }

    # Run the actual page script with a minimal DOM. Reject any attempt to parse
    # nonempty HTML; empty innerHTML is permitted for clearing existing cards.
    harness = r"""
const assert = require('assert');
const vm = require('vm');
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
class Element {
  constructor(tagName) {
    this.tagName = tagName;
    this.children = [];
    this._text = '';
    this.className = '';
    this.classList = {add: (...names) => {
      this.className = [...new Set([...this.className.split(/\s+/).filter(Boolean), ...names])].join(' ');
    }};
  }
  set textContent(value) { this._text = value == null ? '' : String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
  set innerHTML(value) {
    assert.strictEqual(value, '', 'User-controlled values must not be parsed as HTML');
    this.replaceChildren();
  }
  append(...children) {
    for (const child of children) {
      if (typeof child === 'string') {
        const text = new Element('#text');
        text.textContent = child;
        this.children.push(text);
      } else this.children.push(child);
    }
  }
  appendChild(child) { this.append(child); return child; }
  replaceChildren(...children) { this._text = ''; this.children = []; this.append(...children); }
}
global.document = {createElement: tag => new Element(tag)};
global.stats = new Element('span');
global.profiles = new Element('div');
global.feed = new Element('div');
global.fetch = async path => ({json: async () => input.responses[path]});
let refresh;
global.setInterval = callback => { refresh = callback; };

function check() {
  assert.strictEqual(profiles.children.length, 1);
  for (const value of Object.values(input.profile)) {
    assert(profiles.children[0].textContent.includes(value), `Profile value missing: ${value}`);
  }
  assert.strictEqual(feed.children.length, input.posts.length);
  const findCard = post => {
    const card = feed.children.find(element => element.textContent.includes(post.author));
    assert(card, `Post missing: ${post.author}`);
    for (const value of [post.author, post.kind, post.topic, post.body, post.reply_to]) {
      if (value != null && value !== '') assert(card.textContent.includes(String(value)), `Post value missing: ${value}`);
    }
    return card;
  };
  const classes = element => element.className.split(/\s+/).filter(Boolean).sort();
  assert.deepStrictEqual(classes(findCard(input.posts[0])), ['card', 'reply']);
  assert.deepStrictEqual(classes(findCard(input.posts[1])), ['card', 'reply', 'verdict']);
  assert.deepStrictEqual(classes(findCard(input.posts[2])), ['card', 'challenge']);
  assert.deepStrictEqual(classes(findCard(input.posts[3])), ['card']);
  assert(stats.textContent.includes(input.posts[0].kind));
}

(async () => {
  vm.runInThisContext(input.script);
  await new Promise(resolve => setImmediate(resolve));
  check();
  await refresh();
  check();
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    result = subprocess.run([shutil.which("node"), "-e", harness], input=json.dumps(data),
                            text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
