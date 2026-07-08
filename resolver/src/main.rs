// tcb-resolver: in-process typst compile + render + click->source resolution.
// Uses typst's own crates (the same engine as tinymist) — no subprocess, no websocket.
use std::io::BufRead;
use std::num::NonZeroUsize;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::SystemTime;

use typst::diag::{FileError, FileResult};
use typst::foundations::{Bytes, Datetime, Duration};
use typst::introspection::PagedPosition;
use typst::syntax::{FileId, RootedPath, Source, VirtualPath, VirtualRoot};
use typst::text::{Font, FontBook};
use typst::utils::LazyHash;
use typst::{Library, LibraryExt, World};
use typst_ide::{jump_from_click, jump_from_cursor, IdeWorld, Jump};
use typst_kit::downloader::SystemDownloader;
use typst_kit::files::{FileStore, FsRoot, SystemFiles};
use typst_kit::fonts::FontStore;
use typst_kit::packages::SystemPackages;
use typst_layout::PagedDocument;

struct TcbWorld {
    main: FileId,
    library: LazyHash<Library>,
    fonts: FontStore,
    files: Mutex<FileStore<SystemFiles>>,
}

impl TcbWorld {
    fn new(root: PathBuf, main_rel: &str) -> Self {
        let mut fonts = FontStore::new();
        fonts.extend(typst_kit::fonts::embedded());
        fonts.extend(typst_kit::fonts::system());
        // typst-kit handles local files AND @preview packages (download + cache).
        let downloader = SystemDownloader::new("tcb-resolver/0.1");
        let packages = SystemPackages::new(downloader);
        let sys = SystemFiles::new(FsRoot::new(root), packages);
        let files = FileStore::new(sys);
        let vpath = VirtualPath::new(main_rel).expect("bad main path");
        let main = RootedPath::new(VirtualRoot::Project, vpath).intern();
        Self {
            main,
            library: LazyHash::new(Library::builder().build()),
            fonts,
            files: Mutex::new(files),
        }
    }

    // clear caches so the next compile re-reads from disk (comemo still caches layout)
    fn reset(&self) {
        self.files.lock().unwrap().reset();
    }

    fn path_of(&self, id: FileId) -> FileResult<PathBuf> {
        self.files.lock().unwrap().loader().resolve(id)
    }
}

impl World for TcbWorld {
    fn library(&self) -> &LazyHash<Library> {
        &self.library
    }
    fn book(&self) -> &LazyHash<FontBook> {
        self.fonts.book()
    }
    fn main(&self) -> FileId {
        self.main
    }
    fn source(&self, id: FileId) -> FileResult<Source> {
        self.files.lock().unwrap().source(id)
    }
    fn file(&self, id: FileId) -> FileResult<Bytes> {
        self.files.lock().unwrap().file(id)
    }
    fn font(&self, index: usize) -> Option<Font> {
        self.fonts.font(index)
    }
    fn today(&self, _offset: Option<Duration>) -> Option<Datetime> {
        None
    }
}

impl IdeWorld for TcbWorld {
    fn upcast(&self) -> &dyn World {
        self
    }
}

// Format one diagnostic as "message (file:line:col)" when its span resolves to a source.
fn diag_to_string(world: &TcbWorld, d: &typst::diag::SourceDiagnostic) -> String {
    let msg = d.message.to_string();
    if let (Some(id), Some(range)) = (d.span.id(), typst::WorldExt::range(world, d.span)) {
        if let Ok(src) = world.source(id) {
            if let Some((line, col)) = src.lines().byte_to_line_column(range.start) {
                let name = world
                    .path_of(id)
                    .ok()
                    .and_then(|p| p.file_name().map(|f| f.to_string_lossy().to_string()))
                    .unwrap_or_default();
                return format!("{} ({}:{}:{})", msg, name, line + 1, col + 1);
            }
        }
    }
    msg
}

// Ok(doc) on success; Err(messages) with human-readable compile errors otherwise.
fn compile(world: &TcbWorld) -> Result<PagedDocument, Vec<String>> {
    world.reset();
    let result = typst::compile::<PagedDocument>(world);
    match result.output {
        Ok(doc) => Ok(doc),
        Err(errs) => {
            let msgs: Vec<String> = errs.iter().take(5).map(|e| diag_to_string(world, e)).collect();
            for m in &msgs {
                eprintln!("compile error: {}", m);
            }
            Err(if msgs.is_empty() {
                vec!["compile failed".to_string()]
            } else {
                msgs
            })
        }
    }
}

// (page_1based, x_pt, y_pt) -> (file_path, line0, col0)
fn resolve_click(
    world: &TcbWorld,
    doc: &PagedDocument,
    page: usize,
    x_pt: f64,
    y_pt: f64,
) -> Option<(String, usize, usize)> {
    let pos = PagedPosition {
        page: NonZeroUsize::new(page)?,
        point: typst::layout::Point::new(typst::layout::Abs::pt(x_pt), typst::layout::Abs::pt(y_pt)),
    };
    match jump_from_click(world, doc, &pos)? {
        Jump::File(fid, byte_off) => {
            let source = world.source(fid).ok()?;
            let (line, col) = source.lines().byte_to_line_column(byte_off)?;
            let path = world
                .path_of(fid)
                .ok()
                .map(|p| p.display().to_string())
                .unwrap_or_default();
            Some((path, line, col))
        }
        _ => None,
    }
}

// Reverse of resolve_click: a source byte offset -> where it renders, as (page_1based, x_pt,
// y_pt). One source element can appear on several pages (e.g. a touying subslide), so this
// returns every match.
fn resolve_cursor(world: &TcbWorld, doc: &PagedDocument, byte_off: usize) -> Vec<(usize, f64, f64)> {
    let source = match world.source(world.main) {
        Ok(s) => s,
        Err(_) => return vec![],
    };
    let off = byte_off.min(source.text().len());
    jump_from_cursor(doc, &source, off)
        .into_iter()
        .map(|p| (p.page.get(), p.point.x.to_pt(), p.point.y.to_pt()))
        .collect()
}

fn render_svgs(doc: &PagedDocument, dir: &Path) -> usize {
    use typst_svg::SvgOptions;
    let opts = SvgOptions::default();
    let n = doc.pages().len();
    for (i, page) in doc.pages().iter().enumerate() {
        let svg = typst_svg::svg(page, &opts);
        let _ = std::fs::write(dir.join(format!("page-{}.svg", i + 1)), svg);
    }
    // remove stale higher-numbered pages from a previous, longer render
    for k in (n + 1)..(n + 64) {
        let p = dir.join(format!("page-{}.svg", k));
        if p.exists() {
            let _ = std::fs::remove_file(p);
        }
    }
    n
}

fn mtime(p: &Path) -> Option<SystemTime> {
    std::fs::metadata(p).and_then(|m| m.modified()).ok()
}

// Long-running service: compile + render on start and on file change; answer resolve
// queries on stdin. stdout is line-delimited JSON (events + responses).
fn serve(root: PathBuf, main_rel: String, render_dir: PathBuf) {
    let _ = std::fs::create_dir_all(&render_dir);
    let world = Arc::new(TcbWorld::new(root, &main_rel));
    let main_path = world.path_of(world.main).unwrap_or_default();
    let doc: Arc<Mutex<Option<PagedDocument>>> = Arc::new(Mutex::new(None));
    let version = Arc::new(AtomicU64::new(0));

    let recompile = {
        let world = world.clone();
        let doc = doc.clone();
        let version = version.clone();
        let render_dir = render_dir.clone();
        move || {
            match compile(&world) {
                Ok(d) => {
                    let pages = render_svgs(&d, &render_dir);
                    *doc.lock().unwrap() = Some(d);
                    let v = version.fetch_add(1, Ordering::SeqCst) + 1;
                    println!("{{\"event\":\"rendered\",\"version\":{},\"pages\":{}}}", v, pages);
                }
                Err(msgs) => {
                    // keep the LAST good document so the preview still shows something;
                    // the error is surfaced separately so the user knows it is stale.
                    let errs = serde_json::to_string(&msgs).unwrap_or_else(|_| "[]".to_string());
                    println!("{{\"event\":\"compile_error\",\"errors\":{}}}", errs);
                }
            }
        }
    };
    recompile(); // initial

    // watch the main file for changes (the CRDT layer writes it); recompile incrementally
    {
        let main_path = main_path.clone();
        std::thread::spawn(move || {
            let mut last = mtime(&main_path);
            loop {
                std::thread::sleep(std::time::Duration::from_millis(120));
                let cur = mtime(&main_path);
                if cur != last {
                    last = cur;
                    recompile();
                }
            }
        });
    }

    let stdin = std::io::stdin();
    for line in stdin.lock().lines().map_while(Result::ok) {
        let line = line.trim();
        if line.is_empty() {
            continue;
        }
        let req: serde_json::Value = match serde_json::from_str(line) {
            Ok(v) => v,
            Err(_) => continue,
        };
        let id = req.get("id").and_then(|v| v.as_i64()).unwrap_or(0);
        // reverse location: source byte offset -> page coordinates
        if req.get("cmd").and_then(|v| v.as_str()) == Some("cursor") {
            let off = req.get("off").and_then(|v| v.as_u64()).unwrap_or(0) as usize;
            let guard = doc.lock().unwrap();
            let positions = guard
                .as_ref()
                .map(|d| resolve_cursor(&world, d, off))
                .unwrap_or_default();
            drop(guard);
            let arr: Vec<String> = positions
                .iter()
                .map(|(p, x, y)| format!("{{\"page\":{},\"x\":{:.3},\"y\":{:.3}}}", p, x, y))
                .collect();
            println!(
                "{{\"id\":{},\"ok\":{},\"positions\":[{}]}}",
                id,
                !positions.is_empty(),
                arr.join(",")
            );
            continue;
        }
        let page = req.get("page").and_then(|v| v.as_u64()).unwrap_or(1) as usize;
        let x = req.get("x").and_then(|v| v.as_f64()).unwrap_or(0.0);
        let y = req.get("y").and_then(|v| v.as_f64()).unwrap_or(0.0);
        let guard = doc.lock().unwrap();
        let out = match guard.as_ref().and_then(|d| resolve_click(&world, d, page, x, y)) {
            Some((_path, line0, col0)) => {
                format!("{{\"id\":{},\"ok\":true,\"line\":{},\"col\":{}}}", id, line0, col0)
            }
            None => format!("{{\"id\":{},\"ok\":false}}", id),
        };
        drop(guard);
        println!("{}", out);
    }
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    // usage: tcb-resolver <root> <main.typ> serve <render-dir>
    if args.len() >= 5 && args[3] == "serve" {
        serve(PathBuf::from(&args[1]), args[2].clone(), PathBuf::from(&args[4]));
        return;
    }
    // usage: tcb-resolver <root> <main.typ> resolve <page> <x> <y>
    if args.len() >= 7 && args[3] == "resolve" {
        let root = PathBuf::from(&args[1]);
        let world = TcbWorld::new(root, &args[2]);
        let doc = match compile(&world) {
            Ok(d) => d,
            Err(_) => {
                println!("{{\"ok\":false,\"error\":\"compile failed\"}}");
                return;
            }
        };
        let page: usize = args[4].parse().unwrap_or(1);
        let x: f64 = args[5].parse().unwrap_or(0.0);
        let y: f64 = args[6].parse().unwrap_or(0.0);
        match resolve_click(&world, &doc, page, x, y) {
            Some((path, line, col)) => println!(
                "{{\"ok\":true,\"line\":{},\"col\":{},\"file\":{:?}}}",
                line, col, path
            ),
            None => println!("{{\"ok\":false,\"error\":\"no element\"}}"),
        }
        return;
    }
    eprintln!("usage: tcb-resolver <root> <main.typ> resolve <page> <x_pt> <y_pt>");
}
