import sys, http.server, urllib.parse
MODE = sys.argv[2]
HEAD = '<link rel="stylesheet" href="/plugins/system/t3/base/bootstrap/css/bootstrap.css"><link rel="stylesheet" href="/templates/osp_realty/css/template.css"><script src="/media/system/js/mootools-core.js"></script><script src="/templates/osp_realty/js/slick.min.js"></script><meta name="generator" content="Joomla! - Open Source Content Management">'
NEWHEAD = '<link rel="stylesheet" href="/media/templates/site/cassiopeia_wh/css/template.css"><meta name="generator" content="Joomla! - Open Source Content Management">'
def page(title, body_class, links, imgs=(), canon=True, path=""):
    h = HEAD if MODE=="old" else NEWHEAD
    c = f'<link rel="canonical" href="{path}">' if canon else ""
    return f'<html><head><title>{title}</title>{h}{c}</head><body class="{body_class}"><h1>{title}</h1>' + "".join(f'<a href="{l}">x</a>' for l in links) + "".join(f'<img src="{i}">' for i in imgs) + '<a href="mailto:a@b.c">m</a><a href="https://facebook.com/x">fb</a></body></html>'
OLD = {
 "/": ("Western Hunting Properties","com_content view-featured",["/properties","/about-us","/shop","/vip-members","/index.php?option=com_users&view=login","/contact","/index.php?option=com_osproperty&view=property_details&id=12"],["/images/osproperty/properties/12/thumb/a.jpg","/images/banners/home.jpg"]),
 "/properties": ("Hunting Properties for Sale","com_osproperty view-ltype",["/properties/12-tara-station-nsw","/properties/15-bluegum-block-qld","/properties?start=10","/listings/18-old-creek-vic"],["/images/osproperty/properties/12/thumb/a.jpg","/images/osproperty/properties/15/thumb/b.jpg"]),
 "/properties?start=10": ("Hunting Properties for Sale - Page 2","com_osproperty view-ltype",["/properties/18-old-creek-vic"],[]),
 "/properties/12-tara-station-nsw": ("Tara Station NSW 1200 acres","com_osproperty view-property_details",["/properties"],["/images/osproperty/properties/12/a.jpg","/images/osproperty/properties/12/missing.jpg"]),
 "/properties/15-bluegum-block-qld": ("Bluegum Block QLD","com_osproperty view-property_details",["/properties"],["/images/osproperty/properties/15/b.jpg"]),
 "/properties/18-old-creek-vic": ("Old Creek VIC","com_osproperty view-property_details",[],["/images/osproperty/properties/18/c.jpg"]),
 "/about-us": ("About Us","com_content view-article",["/news/hunting-season-2024"],[]),
 "/news/hunting-season-2024": ("Hunting season 2024 update","com_content view-article",["/old-page"],[]),
 "/shop": ("Shop","com_eshop view-frontpage",[],[]),
 "/vip-members": ("VIP Members Area","com_content view-article",[],[]),
 "/index.php?option=com_users&view=login": ("Login","com_users view-login",[],[]),
 "/contact": ("Contact Us","com_contact view-contact",[],[]),
}
REDIR_OLD = {"/index.php?id=12&option=com_osproperty&view=property_details": "/properties/12-tara-station-nsw", "/old-page": "/about-us"}
NEW = dict(OLD)
for k in ["/shop","/vip-members","/index.php?option=com_users&view=login"]: NEW.pop(k)
NEW.pop("/properties/18-old-creek-vic")
NEW["/listings/18-old-creek-vic"] = ("Old Creek VIC","com_osproperty view-property_details",[],[])
NEW["/about-us"] = ("About Western Hunting","com_content view-article",[],[])
REDIR_NEW = {"/shop": (301,"/"), "/index.php?id=12&option=com_osproperty&view=property_details": (302,"/properties/12-tara-station-nsw"), "/old-page": (301,"/about-us")}
IMGS = {"/images/osproperty/properties/12/thumb/a.jpg","/images/osproperty/properties/15/thumb/b.jpg","/images/osproperty/properties/12/a.jpg","/images/osproperty/properties/15/b.jpg","/images/osproperty/properties/18/c.jpg","/images/banners/home.jpg"}
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self,*a): pass
    def key(self):
        p = urllib.parse.urlsplit(self.path); q = urllib.parse.urlencode(sorted(urllib.parse.parse_qsl(p.query)))
        return p.path + ("?"+q if q else "")
    def do_HEAD(self): self.handle_req(False)
    def do_GET(self): self.handle_req(True)
    def handle_req(self, body):
        k = self.key()
        if k == "/robots.txt":
            return self.send(200, f"User-agent: *\nSitemap: http://{self.headers['Host']}/sitemap.xml\n", "text/plain", body)
        if k == "/sitemap.xml" and MODE=="old":
            x = "<urlset>" + "".join(f"<loc>http://{self.headers['Host']}{u}</loc>" for u in ["/", "/properties/18-old-creek-vic", "/contact"]) + "</urlset>"
            return self.send(200, x, "application/xml", body)
        if k in IMGS and not (MODE=="new" and "18/" in k):
            return self.send(200, "img", "image/jpeg", body)
        pages = OLD if MODE=="old" else NEW
        red = {a:(301,b) for a,b in REDIR_OLD.items()} if MODE=="old" else REDIR_NEW
        if k in red:
            s,t = red[k]; self.send_response(s); self.send_header("Location", t); self.end_headers(); return
        if k in pages:
            t,bc,l,i = pages[k]
            return self.send(200, page(t,bc,l,i,canon=(k!="/contact"),path=k), "text/html; charset=utf-8", body)
        self.send(404, "<html><head><title>404</title></head><body>nf</body></html>", "text/html", body)
    def send(self, s, txt, ct, body):
        b = txt.encode(); self.send_response(s); self.send_header("Content-Type", ct); self.send_header("Content-Length", str(len(b))); self.end_headers()
        if body: self.wfile.write(b)
http.server.ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
