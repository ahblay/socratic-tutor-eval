/**
 * graph.js — Live KC knowledge graph panel (Graphviz / viz.js).
 *
 * KCGraph.init(domainMap)        — build graph from domain_map core_concepts
 * KCGraph.setBKT(bktSnapshot)   — colour nodes by knowledge estimate
 * KCGraph.setTutorState(state)  — update observation chips
 */

const KCGraph = (() => {
  let _viz                = null;   // viz.js instance (initialised once)
  let _panZoom            = null;   // svg-pan-zoom instance
  let _concepts           = [];
  let _sequence           = [];     // recommended_sequence from domain map
  let _barEls             = {};  // kept for API compatibility but unused
  let _ready              = false;
  let _bktSnapshot        = {};
  let _activeConceptIndex = -1;     // current_concept_index from tutor state
  let _prevUnderstanding  = [];

  // ── Helpers ───────────────────────────────────────────────────────────────

  function slugify(name) {
    return name.toLowerCase().trim()
      .replace(/[^a-z0-9]+/g, '-')
      .replace(/^-|-$/g, '')
      .slice(0, 64);
  }

  /** Knowledge estimate p → hex colour. */
  function knowledgeColor(p) {
    if (p <= 0.15) return '#1e3a5f';
    const stops = [
      [0.15, [30,  58, 138]],   // deep blue  (low)
      [0.40, [120, 80,   5]],   // amber       (partial)
      [0.70, [22, 101,  52]],   // dark green  (near mastery)
      [1.00, [21, 128,  61]],   // green       (mastered)
    ];
    for (let i = 1; i < stops.length; i++) {
      const [t1, c1] = stops[i - 1];
      const [t2, c2] = stops[i];
      if (p <= t2) {
        const t = (p - t1) / (t2 - t1);
        const r = Math.round(c1[0] + t * (c2[0] - c1[0]));
        const g = Math.round(c1[1] + t * (c2[1] - c1[1]));
        const b = Math.round(c1[2] + t * (c2[2] - c1[2]));
        return `#${r.toString(16).padStart(2,'0')}${g.toString(16).padStart(2,'0')}${b.toString(16).padStart(2,'0')}`;
      }
    }
    return '#15803d';
  }

  /** Word-wrap a label at maxChars per line for Graphviz (uses \n). */
  function wrapLabel(text, maxChars = 16) {
    const words = text.split(' ');
    const lines = [];
    let cur = '';
    for (const w of words) {
      const candidate = cur ? `${cur} ${w}` : w;
      if (candidate.length <= maxChars) {
        cur = candidate;
      } else {
        if (cur) lines.push(cur);
        cur = w;
      }
    }
    if (cur) lines.push(cur);
    return lines.join('\\n');
  }

  // ── DOT source builder ────────────────────────────────────────────────────

  function _buildDot(concepts, bktSnapshot, activeConceptName) {
    const nameSet = new Set(concepts.map(c => c.concept));
    const lines = [
      'digraph G {',
      '  rankdir=TB;',
      '  bgcolor="transparent";',
      '  graph [pad="0.4", nodesep="0.6", ranksep="0.8"];',
      '  node [shape=ellipse, style=filled, fontcolor="#e2e8f0",',
      '        fontname="Helvetica", fontsize=11, margin="0.2,0.12",',
      '        penwidth=1.5];',
      '  edge [color="#64748b", arrowsize=0.7, penwidth=1.2];',
      '',
    ];

    for (const c of concepts) {
      const id    = slugify(c.concept);
      const p     = bktSnapshot[id] ?? 0;
      const fill  = knowledgeColor(p);
      const label = wrapLabel(c.concept);
      // Active: tutor is currently working on this concept
      const isActive  = activeConceptName && c.concept === activeConceptName;
      const seqIdx    = _sequence.indexOf(c.concept);
      // Covered: concept appears before the active index in the sequence
      const isCovered  = !isActive && seqIdx >= 0 && seqIdx < _activeConceptIndex;
      const borderColor = isActive   ? '#ffffff'
                        : isCovered  ? '#22c55e'
                        :              '#3b82f6';
      const penWidth    = isActive   ? '3.5'
                        : isCovered  ? '2.0'
                        :              '1.5';
      lines.push(
        `  "${id}" [label="${label}", fillcolor="${fill}",` +
        ` color="${borderColor}", penwidth=${penWidth}];`
      );
    }

    lines.push('');

    for (const c of concepts) {
      const src = slugify(c.concept);
      for (const target of (c.prerequisite_for || [])) {
        if (!nameSet.has(target)) continue;
        const tgt = slugify(target);
        lines.push(`  "${src}" -> "${tgt}";`);
      }
    }

    lines.push('}');
    return lines.join('\n');
  }

  function prereqsOf(conceptName, concepts) {
    const results = [];
    for (const c of concepts) {
      if ((c.prerequisite_for || []).includes(conceptName)) {
        results.push(c.concept);
      }
    }
    return results;
  }

  function isMastered(conceptName, bktSnapshot, concepts) {
    return (bktSnapshot[slugify(conceptName)] ?? 0) >= 0.7;
  }

  // ── Render ────────────────────────────────────────────────────────────────

  async function _render() {
    if (!_viz || _concepts.length === 0) return;
    const container = document.getElementById('kg-svg');
    if (!container) return;

    // Destroy previous pan-zoom instance before replacing the SVG
    if (_panZoom) {
      try { _panZoom.destroy(); } catch (_) {}
      _panZoom = null;
    }

    const activeConceptName = _activeConceptIndex >= 0 ? _sequence[_activeConceptIndex] : null;
    const dot = _buildDot(_concepts, _bktSnapshot, activeConceptName);
    try {
      const svgEl = _viz.renderSVGElement(dot);
      // Let the SVG fill the container; svg-pan-zoom will handle viewport
      svgEl.setAttribute('width',  '100%');
      svgEl.setAttribute('height', '100%');
      svgEl.style.cssText = 'display:block;';
      container.innerHTML = '';
      container.appendChild(svgEl);

      // Attach pan/zoom (requires SVG to be in the DOM first)
      _panZoom = svgPanZoom(svgEl, {
        zoomEnabled:         true,
        panEnabled:          true,
        controlIconsEnabled: false,
        fit:                 true,
        center:              true,
        minZoom:             0.2,
        maxZoom:             8,
        zoomScaleSensitivity: 0.3,
      });
    } catch (e) {
      console.error('[graph] viz.js render error:', e, '\nDOT:\n', dot);
    }
  }

  // ── init ─────────────────────────────────────────────────────────────────

  async function init(domainMap) {
    _concepts           = (domainMap && domainMap.core_concepts) || [];
    _sequence           = (domainMap && domainMap.recommended_sequence) || [];
    _bktSnapshot        = {};
    _barEls             = {};
    _ready              = false;
    _activeConceptIndex = -1;
    _prevUnderstanding  = [];

    const obsList = document.getElementById('obs-list');
    if (obsList) obsList.innerHTML = '';

    if (_concepts.length === 0) return;

    // Initialise viz.js once (loads the Graphviz WASM)
    if (!_viz) {
      _viz = await Viz.instance();
    }

    await _render();
    _ready = true;
  }

  // ── setBKT ────────────────────────────────────────────────────────────────

  async function setBKT(bktSnapshot) {
    _bktSnapshot = bktSnapshot || {};
    if (_ready) await _render();
  }

  // ── setTutorState ─────────────────────────────────────────────────────────

  async function setTutorState(state) {
    const obsList    = document.getElementById('obs-list');
    const obsSection = document.getElementById('tutor-obs-section');
    if (!obsList || !state) return;

    // Update active concept and re-render graph if index changed
    const newIndex = state.current_concept_index ?? -1;
    if (newIndex !== _activeConceptIndex) {
      _activeConceptIndex = newIndex;
      if (_ready) await _render();
    }

    const understanding = state.student_understanding || [];
    const newItems = understanding.slice(_prevUnderstanding.length);
    _prevUnderstanding = [...understanding];

    for (const text of newItems) {
      const chip = document.createElement('span');
      chip.className = 'obs-chip';
      chip.style.cssText = 'background:#14532d;color:#86efac';
      chip.textContent = text;
      obsList.appendChild(chip);
    }

    const existing = document.getElementById('frustration-chip');
    if (existing) existing.remove();
    const level = state.frustration_level || 'none';
    if (level !== 'none') {
      const colors = {
        mild:     { bg: '#713f12', fg: '#fde68a' },
        moderate: { bg: '#7c2d12', fg: '#fdba74' },
        high:     { bg: '#7f1d1d', fg: '#fca5a5' },
      };
      const col  = colors[level] || { bg: '#1e293b', fg: '#94a3b8' };
      const chip = document.createElement('span');
      chip.id = 'frustration-chip';
      chip.className = 'obs-chip';
      chip.style.cssText = `background:${col.bg};color:${col.fg}`;
      chip.textContent = `Frustration: ${level}`;
      obsList.appendChild(chip);
    }

    if (obsSection) {
      obsSection.classList.remove('hidden');
      const resizeHandle = document.getElementById('obs-resize-handle');
      if (resizeHandle) resizeHandle.classList.remove('hidden');
    }
  }

  return { init, setBKT, setTutorState };
})();
