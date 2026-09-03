#!/usr/bin/env python3
"""Build the Excel default template that opens the Klikk Journals pane by itself.

`AutoOpenTaskpane` in the manifest only re-opens the pane for a workbook that
carries the Office.AutoShowTaskpaneWithDocument flag, and a new blank workbook
carries nothing. Putting the flag in the DEFAULT TEMPLATE is what turns
"opens with a file I prepared" into "opens with Excel".

The webextension parts below are not invented — they mirror what Excel itself
wrote into a workbook on MC's machine, which is where store="developer"
storeType="Registry" and the package-level (_rels/.rels, NOT workbook.xml.rels)
relationship come from. Guessing either one produces a template Excel silently
repairs.

Usage:
    python3 scripts/make_excel_autoopen_template.py [--install]

--install copies it to Excel for Mac's startup folder as Book.xltx, which makes
every new workbook inherit it. Delete that file to undo.
"""
import argparse, pathlib, shutil, re, uuid, zipfile
from openpyxl import Workbook

ADDIN_ID = "5b4e7ec6-3634-49f0-a1a9-a90223523e68"
STARTUP = pathlib.Path.home() / (
    "Library/Group Containers/UBF8T346G9.Office/User Content.localized"
    "/Startup.localized/Excel")


def addin_version(repo_root):
    """Track the manifest — a stale version here can fail to resolve."""
    m = (repo_root / "excel_addin/manifest.xml").read_text()
    return re.search(r"<Version>([^<]+)</Version>", m).group(1)


def build(out: pathlib.Path, version: str):
    base = out.with_suffix(".base.xlsx")
    wb = Workbook()
    wb.template = True
    wb.active.title = "Sheet1"
    wb.save(base)

    webext = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
        '<we:webextension xmlns:we="http://schemas.microsoft.com/office/'
        'webextensions/webextension/2010/11" id="{%s}">'
        '<we:reference id="%s" version="%s" store="developer" storeType="Registry"/>'
        '<we:alternateReferences/><we:properties>'
        '<we:property name="Office.AutoShowTaskpaneWithDocument" value="true"/>'
        '</we:properties><we:bindings/><we:snapshot xmlns:r="http://schemas.'
        'openxmlformats.org/officeDocument/2006/relationships"/></we:webextension>'
    ) % (str(uuid.uuid4()).upper(), ADDIN_ID, version)

    taskpanes = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
        '<wetp:taskpanes xmlns:wetp="http://schemas.microsoft.com/office/'
        'webextensions/taskpanes/2010/11">'
        '<wetp:taskpane dockstate="right" visibility="1" width="350" row="0">'
        '<wetp:webextensionref xmlns:r="http://schemas.openxmlformats.org/'
        'officeDocument/2006/relationships" r:id="rId1"/></wetp:taskpane>'
        '</wetp:taskpanes>')

    tp_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
        'relationships"><Relationship Id="rId1" Type="http://schemas.microsoft.'
        'com/office/2011/relationships/webextension" Target="webextension1.xml"/>'
        '</Relationships>')

    with zipfile.ZipFile(base) as zin, \
            zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
        for name in zin.namelist():
            data = zin.read(name)
            if name == "[Content_Types].xml":
                t = data.decode()
                t = t.replace("</Types>",
                    '<Override PartName="/xl/webextensions/taskpanes.xml" '
                    'ContentType="application/vnd.ms-office.webextensiontaskpanes+xml"/>'
                    '<Override PartName="/xl/webextensions/webextension1.xml" '
                    'ContentType="application/vnd.ms-office.webextension+xml"/></Types>')
                data = t.encode()
            elif name == "_rels/.rels":
                t = data.decode()
                used = [int(x) for x in re.findall(r'Id="rId(\d+)"', t)]
                t = t.replace("</Relationships>",
                    '<Relationship Id="rId%d" Type="http://schemas.microsoft.com/'
                    'office/2011/relationships/webextensiontaskpanes" '
                    'Target="xl/webextensions/taskpanes.xml"/></Relationships>'
                    % (max(used, default=0) + 1))
                data = t.encode()
            zout.writestr(name, data)
        zout.writestr("xl/webextensions/taskpanes.xml", taskpanes)
        zout.writestr("xl/webextensions/webextension1.xml", webext)
        zout.writestr("xl/webextensions/_rels/taskpanes.xml.rels", tp_rels)
    base.unlink()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--install", action="store_true")
    ap.add_argument("--out", default="Book.xltx")
    args = ap.parse_args()

    repo = pathlib.Path(__file__).resolve().parent.parent
    version = addin_version(repo)
    out = pathlib.Path(args.out)
    build(out, version)
    print("built %s for add-in version %s" % (out, version))

    if args.install:
        STARTUP.mkdir(parents=True, exist_ok=True)
        shutil.copy(out, STARTUP / "Book.xltx")
        print("installed -> %s" % (STARTUP / "Book.xltx"))
