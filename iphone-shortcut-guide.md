# Send any playing video to the downloader (iPhone Shortcut)

## What this does
Some pages (YouTube, Instagram, Terabox) hide their video addresses from
servers — but your Safari can see them because the video is playing right
there. This Shortcut grabs the address from inside your Safari and opens
our downloader with the link already filled in.

## Build it (one time, about 3 minutes)
1. Open the **Shortcuts** app → tap **+** for a new shortcut.
2. **Add Action** → search **Run JavaScript on Web Page** → add it.
3. Delete the sample code in the box and paste this exactly:

```javascript
var urls = [];
function add(u){
  if(!u) return;
  if(u.indexOf("blob:")===0) return;  // blob URLs can't leave the page
  if(u.indexOf("http")!==0) return;
  if(urls.indexOf(u)>=0) return;
  urls.push(u);
}
// 1. Currently-playing video elements (best source)
var vs = document.querySelectorAll("video");
for (var i=0;i<vs.length;i++){
  add(vs[i].currentSrc || vs[i].src);
  var ss = vs[i].querySelectorAll("source");
  for (var k=0;k<ss.length;k++) add(ss[k].src);
}
// 2. All <source> tags on the page
var allS = document.querySelectorAll("source[src]");
for (var j=0;j<allS.length;j++) add(allS[j].src);
// 3. Open-Graph video tags (many sites, incl. Terabox previews)
var ogs = document.querySelectorAll(
  'meta[property="og:video:secure_url"], meta[property="og:video:url"], meta[property="og:video"]'
);
for (var m=0;m<ogs.length;m++) add(ogs.getAttribute("content"));
// 4. Direct file links (<a> tags pointing at video files)
var links = document.querySelectorAll("a[href]");
for (var l=0;l<links.length;l++){
  var h = links[l].href || "";
  if (/\.(mp4|mkv|webm|mov|m4v)(\?|#|$)/i.test(h)) add(h);
}
// 5. Terabox / generic: video URLs hidden in the page's scripts
var html = document.documentElement.innerHTML;
var re = /https?:\/\/[^"'\\\s<>]+\.(mp4|mkv|webm|mov|m4v)(\?[^"'\\\s<>]*)?/gi;
var mm;
while ((mm = re.exec(html)) !== null) add(mm[0]);
// Prefer the longest URL (usually the real file, not a thumbnail)
urls.sort(function(a,b){ return b.length - a.length; });
completion(urls);
```

4. **Add Action** → **Choose from List**.
5. **Add Action** → **URL Encode** (uses the chosen item automatically).
6. **Add Action** → **Text**. Type exactly this:
   `https://video-downloader-vgdj9xw7eceyjq9zkpd5re.streamlit.app/?url=`
   then tap the variables bar above the keyboard and insert **URL Encoded Text**
   right after the `=`.
7. **Add Action** → **Open URLs** (uses the text automatically).
8. Tap the name at the top → rename it **Send video to downloader**.
9. Tap the **ⓘ** at the bottom → turn on **Show in Share Sheet**.

## Use it
1. In Safari, open the page with the video.
2. **For Terabox:** tap the file so the video starts playing in their preview
   player, then run the Shortcut while it's playing.
3. Tap **Share** → **Send video to downloader**.
4. Pick the video from the list (the longest address is usually the right one).
5. Our site opens with the link filled in and starts fetching automatically —
   pick a quality, then tap **Download to iPhone** to save it.

## Honest limits
- If the Shortcut finds no addresses, the player uses a scrambled, blob, or
  protected stream. Nothing can grab those — not the site, not the Shortcut.
- Terabox links expire quickly — use the address within a few minutes.
- If Terabox asks you to log in before playing, that's their wall; the
  Shortcut can only grab what Safari can play without login.
