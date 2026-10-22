// Bed mesh panel: the mesh as a 3D surface (Three.js, loaded on first use) or a colour map (ECharts heatmap), range,
// profiles (load / save / remove), calibrate, clear. 3D: drag = rotate, right-drag = pan, wheel = zoom, double-click = reset.
PB.panel("bedmesh", {
  title: "Bed mesh", desc: "Bed mesh map, profiles and calibrate", cls: "adv",
  icon: PB.icon('<path d="M3 17l9 4 9-4M3 12l9 4 9-4M3 7l9 4 9-4-9-4z"/>'),
  settings: [{ k: "probed", label: "Show the probed points (not the smoothed mesh)", type: "check", def: false }],
  create(host, cfg) {
    host.innerHTML = `<div class="pb-tool"><select data-a="prof" aria-label="Mesh profile"></select><button type="button" data-a="load">Load</button>
        <span class="pb-meshmode" role="group" aria-label="Mesh view"><button type="button" data-m="3d">3D</button><button type="button" data-m="2d">2D</button></span>
        <span class="bd-sp"></span><span class="pb-range pb-muted"></span></div>
      <div class="pb-chart pb-mesh"></div><div class="pb-mesh3d" hidden><span class="pb-meshz pb-muted"></span></div>
      <div class="pb-row"><button type="button" data-a="cal">Calibrate</button><button type="button" data-a="clear">Clear</button>
        <button type="button" data-a="save">Save as…</button><button type="button" data-a="rm">Remove</button></div>`;
    const q = s => host.querySelector(s), chartEl = q(".pb-mesh"), g3 = q(".pb-mesh3d");
    let chart = null, key = "", printing = false, last = null, three = null;
    let mode = PB.store.get("pb-meshmode", "3d");
    if (window.echarts) { chart = echarts.init(chartEl); new ResizeObserver(() => chart.resize()).observe(chartEl); }
    new ResizeObserver(() => draw3d()).observe(g3);
    for (const b of q(".pb-meshmode").querySelectorAll("button")) b.onclick = () => setMode(b.dataset.m);
    function setMode(m) {
      mode = m; PB.store.set("pb-meshmode", m);
      for (const b of q(".pb-meshmode").querySelectorAll("button")) b.setAttribute("aria-pressed", b.dataset.m === m);
      chartEl.hidden = m === "3d"; g3.hidden = m !== "3d";
      if (m === "2d") chart?.resize();
      if (last) draw(last);
    }
    async function load() {
      if (pageId === null) return;
      try {
        const r = await PB.rpc("printer.objects.query", { objects: { bed_mesh: null } });
        draw(r?.status?.bed_mesh || {});
      } catch (e) { q(".pb-range").textContent = e.message; }
    }
    function draw(m) {
      last = m;
      const z = (cfg.probed ? m.probed_matrix : m.mesh_matrix) || [];
      const sel = q("[data-a=prof]"), profs = Object.keys(m.profiles || {});
      sel.innerHTML = profs.map(n => `<option${n === m.profile_name ? " selected" : ""}>${esc(n)}</option>`).join("") || `<option value="">No saved profiles</option>`;
      if (!z.length || !z[0]?.length) {
        q(".pb-range").textContent = "No mesh loaded"; chart?.clear(); surface(null);
        if (!chart) chartEl.innerHTML = `<p class="pb-muted">No mesh loaded.</p>`;
        return;
      }
      const flat = z.flat(), lo = Math.min(...flat), hi = Math.max(...flat);
      q(".pb-range").textContent = `${m.profile_name ? m.profile_name + " · " : ""}range ${(hi - lo).toFixed(3)} mm (${lo.toFixed(3)} to ${hi.toFixed(3)})`;
      const [x0, y0] = m.mesh_min || [0, 0], [x1, y1] = m.mesh_max || [z[0].length - 1, z.length - 1];
      if (mode === "3d") return surface({ z, x0, y0, x1, y1, lo, hi });
      if (!chart) return;
      const xs = z[0].map((_, i) => (x0 + (x1 - x0) * i / Math.max(1, z[0].length - 1)).toFixed(0));
      const ys = z.map((_, j) => (y0 + (y1 - y0) * j / Math.max(1, z.length - 1)).toFixed(0));
      const lim = Math.max(Math.abs(lo), Math.abs(hi), 0.05);
      chart.setOption({
        backgroundColor: "transparent", animation: false, textStyle: { color: "#8F8B99", fontSize: 11 },
        grid: { left: 40, right: 60, top: 10, bottom: 26 },
        tooltip: { formatter: p => `X ${xs[p.value[0]]} · Y ${ys[p.value[1]]}<br><b>${p.value[2].toFixed(3)} mm</b>` },
        xAxis: { type: "category", data: xs, splitArea: { show: false } }, yAxis: { type: "category", data: ys },
        visualMap: { min: -lim, max: lim, calculable: true, orient: "vertical", right: 0, top: "middle", itemHeight: 260,
                     textStyle: { color: "#8F8B99" }, inRange: { color: ["#3b82f6", "#1c1c1f", "#ff8a1f"] }, formatter: v => v.toFixed(2) },
        series: [{ type: "heatmap", data: z.flatMap((row, j) => row.map((v, i) => [i, j, v])),
                   label: { show: z[0].length <= 9, formatter: p => p.value[2].toFixed(2), color: "#e8e6ec", fontSize: 10 } }],
      }, true);
    }
    const bm = (text, cmd) => PB.confirmGcode(text, cmd, "Sent");
    q("[data-a=load]").onclick = () => { const n = q("[data-a=prof]").value; if (n) gcode(`BED_MESH_PROFILE LOAD="${n}"`); };
    q("[data-a=cal]").onclick = () => bm("Probe a new bed mesh now? The printer must be homed (and hot, if you mesh hot).", "BED_MESH_CALIBRATE");
    q("[data-a=clear]").onclick = () => bm("Clear the active mesh?", "BED_MESH_CLEAR");
    q("[data-a=save]").onclick = () => { const n = prompt("Save the current mesh as profile:", "default"); if (n) bm(`Save as "${n}"? Then run SAVE_CONFIG when idle.`, `BED_MESH_PROFILE SAVE="${n}"`); };
    q("[data-a=rm]").onclick = () => { const n = q("[data-a=prof]").value; if (n) bm(`Remove profile "${n}"? Then run SAVE_CONFIG when idle.`, `BED_MESH_PROFILE REMOVE="${n}"`); };
    // ---- 3D surface ----------------------------------------------------------------------------
    const loadThree = () => window.THREE ? Promise.resolve(window.THREE) : (PB._three ||= new Promise((ok, bad) => {
      const sc = document.createElement("script"); sc.src = "/static/vendor/three.min.js";
      sc.onload = () => ok(window.THREE); sc.onerror = () => { PB._three = null; bad(new Error("couldn't load the 3D library")); };
      document.head.appendChild(sc);
    }));
    // same colours as the 2D map: blue (low) - dark - orange (high)
    const ramp = [[0x3b, 0x82, 0xf6], [0x1c, 0x1c, 0x1f], [0xff, 0x8a, 0x1f]];
    function colour(t) {   // t: -1..1
      const a = t < 0 ? ramp[0] : ramp[2], u = Math.min(1, Math.abs(t)), b = ramp[1];
      return b.map((v, i) => (v + (a[i] - v) * u) / 255);
    }
    async function surface(d) {
      if (!d) { if (three) { clearMesh(); draw3d(); } return; }
      if (!three) {
        let T; try { T = await loadThree(); } catch (e) { q(".pb-meshz").textContent = `3D unavailable: ${e.message}`; return; }
        const renderer = new T.WebGLRenderer({ antialias: true, alpha: true });
        renderer.setPixelRatio(Math.min(2, devicePixelRatio || 1)); g3.prepend(renderer.domElement);
        const scene = new T.Scene(), camera = new T.PerspectiveCamera(40, 1, 0.5, 5000); camera.up.set(0, 0, 1);
        scene.add(new T.HemisphereLight(0xffffff, 0x444450, 0.9));
        const sun = new T.DirectionalLight(0xffffff, 0.6); camera.add(sun); sun.position.set(0.4, 1, 0.6); scene.add(camera);
        three = { T, renderer, scene, camera, objs: [], key: "", orbit: { th: 0, ph: 1, r: 400, tx: 0, ty: 0, tz: 0 } };
        PB.parts.gcodeOrbit(renderer.domElement, { get three() { return three; }, draw3d, home3d });   // gcodeview/orbit.js
      }
      const { T } = three, { z, x0, y0, x1, y1, lo, hi } = d, nx = z[0].length, ny = z.length;
      const span = Math.max(x1 - x0, y1 - y0, 1), lim = Math.max(Math.abs(lo), Math.abs(hi), 0.05);
      // the bed is ~250 mm across and the mesh well under 1 mm high: stretch Z so the shape shows
      const k = Math.max(1, Math.round(span * 0.12 / Math.max(hi - lo, 0.05)));
      q(".pb-meshz").textContent = `height x${k}`;
      clearMesh();
      const pos = [], col = [], idx = [];
      for (let j = 0; j < ny; j++) for (let i = 0; i < nx; i++) {
        pos.push(x0 + (x1 - x0) * i / Math.max(1, nx - 1), y0 + (y1 - y0) * j / Math.max(1, ny - 1), z[j][i] * k);
        col.push(...colour(z[j][i] / lim));
      }
      for (let j = 0; j + 1 < ny; j++) for (let i = 0; i + 1 < nx; i++) {
        const a = j * nx + i, b = a + 1, c = a + nx, e = c + 1;
        idx.push(a, b, e, a, e, c);
      }
      const geo = new T.BufferGeometry();
      geo.setAttribute("position", new T.Float32BufferAttribute(pos, 3)); geo.setAttribute("color", new T.Float32BufferAttribute(col, 3));
      geo.setIndex(idx); geo.computeVertexNormals();
      const add = o => { three.objs.push(o); three.scene.add(o); };
      add(new T.Mesh(geo, new T.MeshPhongMaterial({ vertexColors: true, side: T.DoubleSide, shininess: 30 })));
      add(new T.LineSegments(new T.WireframeGeometry(geo), new T.LineBasicMaterial({ color: 0xe8e6ec, transparent: true, opacity: 0.18 })));
      const edge = new T.BufferGeometry();   // the flat bed (Z 0) under it
      edge.setAttribute("position", new T.Float32BufferAttribute([x0, y0, 0, x1, y0, 0, x1, y1, 0, x0, y1, 0], 3));
      add(new T.LineLoop(edge, new T.LineBasicMaterial({ color: 0x8f8b99, transparent: true, opacity: 0.6 })));
      // X along the front edge, Y along the left edge: ticks in mm and the axis names
      const gap = span * 0.07, size = span * 0.06, tick = [];
      for (const f of [0, 0.5, 1]) {
        const x = x0 + (x1 - x0) * f, y = y0 + (y1 - y0) * f;
        add(label(x.toFixed(0), x, y0 - gap, size, "#8f8b99"));
        add(label(y.toFixed(0), x0 - gap, y, size, "#8f8b99"));
        tick.push(x, y0, 0, x, y0 - gap * 0.4, 0, x0, y, 0, x0 - gap * 0.4, y, 0);
      }
      add(label("X", (x0 + x1) / 2, y0 - gap * 2.2, size * 1.3, "#e8e6ec"));
      add(label("Y", x0 - gap * 2.2, (y0 + y1) / 2, size * 1.3, "#e8e6ec"));
      const tg = new T.BufferGeometry(); tg.setAttribute("position", new T.Float32BufferAttribute(tick, 3));
      add(new T.LineSegments(tg, new T.LineBasicMaterial({ color: 0x8f8b99, transparent: true, opacity: 0.6 })));
      // hover marker: the mesh point under the pointer
      const dot = new T.Mesh(new T.SphereGeometry(span * 0.012, 12, 8), new T.MeshBasicMaterial({ color: 0xffffff }));
      dot.visible = false; add(dot);
      Object.assign(three, { surf: three.objs[0], dot, d, k });
      const box = `${x0},${y0},${x1},${y1}`;
      if (three.key !== box) { three.key = box; three.box = [x0, y0, x1, y1]; home3d(); }
      draw3d();
    }
    function clearMesh() {
      for (const o of three.objs) { three.scene.remove(o); o.geometry.dispose(); o.material.map?.dispose(); o.material.dispose(); }
      three.objs = []; three.surf = three.dot = null; tip.hidden = true;
    }
    function label(text, x, y, h, color) {   // flat text that always faces the camera
      const { T } = three, c = document.createElement("canvas"), g = c.getContext("2d");
      g.font = "600 44px system-ui, sans-serif";
      c.width = Math.ceil(g.measureText(text).width) + 16; c.height = 64;
      g.font = "600 44px system-ui, sans-serif"; g.fillStyle = color; g.textAlign = "center"; g.textBaseline = "middle";
      g.fillText(text, c.width / 2, 34);
      const tex = new T.CanvasTexture(c); tex.minFilter = T.LinearFilter;
      const s = new T.Sprite(new T.SpriteMaterial({ map: tex, transparent: true, depthWrite: false }));
      s.scale.set(h * c.width / c.height, h, 1); s.position.set(x, y, 0);
      return s;
    }
    // hover: nearest mesh point to where the pointer meets the surface
    const tip = document.createElement("div"); tip.className = "pb-meshtip"; tip.hidden = true; g3.append(tip);
    g3.addEventListener("pointermove", e => {
      if (!three?.surf || e.buttons) { hover(null); return; }
      const r = three.renderer.domElement.getBoundingClientRect(), { T } = three;
      const ray = new T.Raycaster(); ray.setFromCamera(new T.Vector2((e.clientX - r.left) / r.width * 2 - 1, -(e.clientY - r.top) / r.height * 2 + 1), three.camera);
      const hit = ray.intersectObject(three.surf)[0];
      if (!hit) { hover(null); return; }
      const { z, x0, y0, x1, y1 } = three.d, nx = z[0].length, ny = z.length;
      const i = Math.round((hit.point.x - x0) / ((x1 - x0) || 1) * (nx - 1)), j = Math.round((hit.point.y - y0) / ((y1 - y0) || 1) * (ny - 1));
      hover({ i: Math.max(0, Math.min(nx - 1, i)), j: Math.max(0, Math.min(ny - 1, j)), px: e.clientX - r.left, py: e.clientY - r.top });
    });
    g3.addEventListener("pointerleave", () => hover(null));
    function hover(h) {
      if (!three?.dot) return;
      if (!h) { if (three.dot.visible || !tip.hidden) { three.dot.visible = false; tip.hidden = true; draw3d(); } return; }
      const { z, x0, y0, x1, y1 } = three.d, nx = z[0].length, ny = z.length;
      const x = x0 + (x1 - x0) * h.i / Math.max(1, nx - 1), y = y0 + (y1 - y0) * h.j / Math.max(1, ny - 1), v = z[h.j][h.i];
      three.dot.position.set(x, y, v * three.k); three.dot.visible = true;
      tip.innerHTML = `X ${x.toFixed(0)} · Y ${y.toFixed(0)}<br><b>${v >= 0 ? "+" : ""}${v.toFixed(3)} mm</b>`;
      tip.hidden = false;
      tip.style.left = `${Math.min(h.px + 14, g3.clientWidth - tip.offsetWidth - 4)}px`; tip.style.top = `${Math.max(4, h.py - tip.offsetHeight - 10)}px`;
      draw3d();
    }
    function home3d() {   // front-left, looking down at the middle
      const [x0, y0, x1, y1] = three.box || [0, 0, 250, 250];
      Object.assign(three.orbit, { th: -Math.PI / 2 - 0.5, ph: 0.75, r: Math.max(x1 - x0, y1 - y0) * 2.05, tx: (x0 + x1) / 2, ty: (y0 + y1) / 2, tz: 0 });
    }
    function draw3d() {
      if (!three || g3.hidden) return;
      const r = g3.getBoundingClientRect(); if (!r.width) return;
      three.renderer.setSize(r.width, r.height, false); three.camera.aspect = r.width / r.height; three.camera.updateProjectionMatrix();
      const o = three.orbit, cam = three.camera;
      cam.position.set(o.tx + o.r * Math.sin(o.ph) * Math.cos(o.th), o.ty + o.r * Math.sin(o.ph) * Math.sin(o.th), o.tz + o.r * Math.cos(o.ph));
      cam.lookAt(o.tx, o.ty, o.tz);
      three.renderer.render(three.scene, cam);
    }
    setMode(mode);
    return {
      open() { key = ""; },
      shown() { load(); chart?.resize(); draw3d(); },
      update(p) {
        const k = JSON.stringify(p.mesh);
        if (k !== key) { key = k; if (host.offsetParent) load(); }
        printing = p.state === "printing" || p.state === "paused";
        const live = p.connection === "online" && p.klippy_state === "ready";
        for (const b of host.querySelectorAll("button")) b.disabled = !live || (printing && b.dataset.a !== "load");
        if (!p.features?.bed_mesh) q(".pb-range").textContent = "No [bed_mesh] in printer.cfg";
      },
      destroy() { chart?.dispose(); if (three) { clearMesh(); three.renderer.dispose(); three = null; } },
    };
  },
});
