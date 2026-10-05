# Send any playing video to the downloader (iPhone Shortcut)

## What this does
Some pages build their video's address while the page runs, using scripts.
The website can't see those addresses — but your Safari can, because the
video is playing right there. This Shortcut grabs the address from inside
your Safari and opens our downloader with the link already filled in.

## Build it (one time, about 3 minutes)
1. Open the **Shortcuts** app → tap **+** for a new shortcut.
2. **Add Action** → search **Run JavaScript on Web Page** → add it.
3. Delete the sample code in the box and paste this exactly:

```javascript
var urls = [];
function add(u){ if(u && u.indexOf("http")===0 && urls.indexOf(u)<0) urls.push(u); }
var vs = document.querySelectorAll("video");
for (var i=0;i<vs.length;i++){ add(vs[i].currentSrc || vs[i].src); }
var ss = document.querySelectorAll("source");
for (var j=0;j<ss.length;j++){ add(ss[j].src); }
var og = document.querySelector('meta[property="og:video:secure_url"], meta[property="og:video"]');
if (og) add(og.getAttribute("content"));
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
1. In Safari, open any page with a video.
2. Tap **Share** → **Send video to downloader**.
3. Pick the video from the list (if there is more than one).
4. Our site opens with the link filled in and Direct link mode selected —
   tap **Download**, then open the file link to save it.

## Honest limits
- If the Shortcut finds no addresses, the player uses a scrambled or
  protected stream. Nothing can grab those — not the site, not the Shortcut.
- Links found this way can expire after a while, so use them soon.
