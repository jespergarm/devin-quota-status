"""Build dist\\devin-quota-status-<version>.vsix for the Devin Quota
Status extension. A VSIX is a plain zip: [Content_Types].xml,
extension.vsixmanifest, and the files under extension/. Stdlib only —
no npm/vsce needed.

Usage: python build_vsix.py
"""

import json
import os
import zipfile
from xml.sax.saxutils import escape

ROOT = os.path.dirname(os.path.abspath(__file__))
EXT_DIR = os.path.join(ROOT, "extension")
DIST = os.path.join(ROOT, "dist")

CONTENT_TYPES = """<?xml version="1.0" encoding="utf-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="json" ContentType="application/json"/>
<Default Extension="js" ContentType="application/javascript"/>
<Default Extension="py" ContentType="text/plain"/>
<Default Extension="md" ContentType="text/markdown"/>
<Default Extension="vsixmanifest" ContentType="text/xml"/>
</Types>
"""

MANIFEST = """<?xml version="1.0" encoding="utf-8"?>
<PackageManifest Version="2.0.0" xmlns="http://schemas.microsoft.com/developer/vsx-schema/2011" xmlns:d="http://schemas.microsoft.com/developer/vsx-schema-design/2011">
<Metadata>
<Identity Language="en-US" Id="{name}" Version="{version}" Publisher="{publisher}"/>
<DisplayName>{display}</DisplayName>
<Description xml:space="preserve">{desc}</Description>
<Categories>Other</Categories>
<Properties>
<Property Id="Microsoft.VisualStudio.Code.Engine" Value="{engine}"/>
<Property Id="Microsoft.VisualStudio.Code.ExtensionKind" Value="ui,workspace"/>
</Properties>
</Metadata>
<Installation>
<InstallationTarget Id="Microsoft.VisualStudio.Code"/>
</Installation>
<Dependencies/>
<Assets>
<Asset Type="Microsoft.VisualStudio.Code.Manifest" Path="extension/package.json" Addressable="true"/>
<Asset Type="Microsoft.VisualStudio.Services.Content.Details" Path="extension/README.md" Addressable="true"/>
</Assets>
</PackageManifest>
"""


def main():
    with open(os.path.join(EXT_DIR, "package.json"),
              encoding="utf-8") as f:
        pkg = json.load(f)
    manifest = MANIFEST.format(
        name=escape(pkg["name"]),
        version=escape(pkg["version"]),
        publisher=escape(pkg["publisher"]),
        display=escape(pkg["displayName"]),
        desc=escape(pkg.get("description", pkg["displayName"])),
        engine=escape(pkg["engines"]["vscode"]))
    os.makedirs(DIST, exist_ok=True)
    out = os.path.join(DIST, f"{pkg['name']}-{pkg['version']}.vsix")
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", CONTENT_TYPES)
        z.writestr("extension.vsixmanifest", manifest)
        for dirpath, dirs, files in os.walk(EXT_DIR):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for fn in sorted(files):
                if fn.endswith((".pyc", ".pyo")):
                    continue
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, ROOT).replace("\\", "/")
                z.write(full, rel)
    print(out)


if __name__ == "__main__":
    main()
