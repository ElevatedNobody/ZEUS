(function () {
  "use strict";
  const M = window.ZEUS;

  let isAutoCruise = false;
  let fluidImpulse = 0.0;
  let targetZoom = 0.0;
  let continuousZoom = 0.0;
  
  let scene, camera, renderer, bgParticleSystem;
  let octaves = [];
  const OCTAVE_COUNT = 4;
  const SCALE_STEP = 6.0;
  const PARTICLES_PER_OCTAVE = 8500;
  let infiniteRootGroup;
  
  M.wave = {
    attach() {
      const existing = document.getElementById("wave") || document.getElementById("webgl-viewport");
      if (existing) return existing;
      const canvas = document.createElement("canvas");
      canvas.id = "wave";
      canvas.style.cssText = "position:fixed;inset:0;width:100vw;height:100vh;z-index:0;display:block;pointer-events:none;";
      const vignette = document.createElement("div");
      vignette.className = "vignette";
      vignette.style.cssText = "position:fixed;inset:0;z-index:1;pointer-events:none;";
      document.body.prepend(canvas, vignette);
      
      try { initThree(canvas); } catch (e) { console.error("WebGL init error:", e); }
      return canvas;
    },
    mode(name) {
      document.body.dataset.wave = name || "home";
    },
    state(name) {
      if (name === "LISTENING" || name === "THINKING") {
        fluidImpulse = Math.max(fluidImpulse, 0.8);
      }
    },
    level(value) {
      if (value > 0.1) {
        fluidImpulse = Math.max(fluidImpulse, value);
      }
    },
    surge(ms) {
      fluidImpulse = 1.5;
    },
    wake(ms) {},
    sleep() {},
    running: () => true,
    toggleCruise() {
      isAutoCruise = !isAutoCruise;
      return isAutoCruise;
    },
    zoomIn() {
      targetZoom += 1.0;
      fluidImpulse = 0.8;
    },
    zoomOut() {
      targetZoom -= 1.0;
      fluidImpulse = 0.8;
    },
    reseed() {
      octaves.forEach(oct => oct.reseed());
      fluidImpulse = 1.0;
    },
    triggerImpulse() {
      fluidImpulse = 1.2;
    }
  };

  function createGlowParticleTexture() {
    const pCanvas = document.createElement('canvas');
    pCanvas.width = 128;
    pCanvas.height = 128;
    const ctx = pCanvas.getContext('2d');

    const grad = ctx.createRadialGradient(64, 64, 0, 64, 64, 64);
    grad.addColorStop(0, 'rgba(255, 255, 255, 1.0)');
    grad.addColorStop(0.2, 'rgba(255, 240, 200, 0.85)');
    grad.addColorStop(0.5, 'rgba(212, 175, 55, 0.45)');
    grad.addColorStop(0.85, 'rgba(0, 229, 255, 0.12)');
    grad.addColorStop(1.0, 'rgba(0, 0, 0, 0)');

    ctx.fillStyle = grad;
    ctx.fillRect(0, 0, 128, 128);

    const texture = new THREE.CanvasTexture(pCanvas);
    texture.needsUpdate = true;
    return texture;
  }

  const MORPHOLOGY_TYPES = [
    {
      name: "HYDROCARBON VORTEX α",
      func: (i, count, seed) => {
        const frac = i / count;
        const angle = frac * Math.PI * 2 * 9.0 + seed;
        const r = Math.pow(frac, 0.55) * 4.8 + 0.3;
        const x = Math.cos(angle) * r + (Math.random() - 0.5) * 0.3;
        const y = Math.sin(angle) * r + (Math.random() - 0.5) * 0.3;
        const z = Math.sin(frac * Math.PI * 8.0 + seed) * 1.2 + (Math.random() - 0.5) * 0.6;
        return [x, y, z];
      }
    },
    {
      name: "COLLOIDAL OBSIDIAN FILAMENT β",
      func: (i, count, seed) => {
        const t = (i / count) * Math.PI * 14.0;
        const r = 2.8 + Math.sin(t * 3.0 + seed) * 1.6;
        const x = r * Math.cos(t) + (Math.random() - 0.5) * 0.45;
        const y = r * Math.sin(t) + (Math.random() - 0.5) * 0.45;
        const z = Math.cos(t * 5.0 + seed) * 2.2 + (Math.random() - 0.5) * 0.5;
        return [x, y, z];
      }
    },
    {
      name: "GASOLINE NEBULAR CLUSTER γ",
      func: (i, count, seed) => {
        const u = Math.random();
        const r = Math.pow(u, 2.2) * 5.2 + 0.1;
        const theta = Math.random() * Math.PI * 2;
        const phi = Math.acos(2 * Math.random() - 1);
        const x = r * Math.sin(phi) * Math.cos(theta);
        const y = r * Math.sin(phi) * Math.sin(theta) * 0.8;
        const z = r * Math.cos(phi) * 1.4;
        return [x, y, z];
      }
    },
    {
      name: "QUANTUM HYDROCARBON TOROID δ",
      func: (i, count, seed) => {
        const u = Math.random() * Math.PI * 2;
        const v = Math.random() * Math.PI * 2;
        const R = 3.2 + Math.sin(u * 2.0 + seed) * 0.8;
        const r = 1.1 + Math.cos(v * 3.0 + seed) * 0.4;
        const x = (R + r * Math.cos(v)) * Math.cos(u);
        const y = (R + r * Math.cos(v)) * Math.sin(u);
        const z = r * Math.sin(v) * 2.0 + (Math.random() - 0.5) * 0.3;
        return [x, y, z];
      }
    },
    {
      name: "AURELIA DENSITY WAVE ε",
      func: (i, count, seed) => {
        const arms = 5;
        const arm = i % arms;
        const frac = Math.floor(i / arms) / (count / arms);
        const angle = (arm * (Math.PI * 2 / arms)) + frac * 6.0 + seed;
        const r = frac * 5.0 + 0.2;
        const x = Math.cos(angle) * r + (Math.random() - 0.5) * 0.25;
        const y = Math.sin(angle) * r + (Math.random() - 0.5) * 0.25;
        const z = Math.sin(frac * 12.0) * 0.9 + (Math.random() - 0.5) * 0.4;
        return [x, y, z];
      }
    }
  ];

  const palettePetroleum = [
    new THREE.Color('#d4af37'), new THREE.Color('#ffaa00'), new THREE.Color('#00e5ff'),
    new THREE.Color('#0ea5e9'), new THREE.Color('#b353ff'), new THREE.Color('#05cd99'), new THREE.Color('#1c2d42')
  ];

  class OctaveLayer {
    constructor(index, particleTexture) {
      this.index = index;
      this.generationCount = 0;
      this.group = new THREE.Group();
      this.geo = new THREE.BufferGeometry();
      this.positions = new Float32Array(PARTICLES_PER_OCTAVE * 3);
      this.colors = new Float32Array(PARTICLES_PER_OCTAVE * 3);
      this.baseCoordinates = new Float32Array(PARTICLES_PER_OCTAVE * 3);

      this.geo.setAttribute('position', new THREE.BufferAttribute(this.positions, 3));
      this.geo.setAttribute('color', new THREE.BufferAttribute(this.colors, 3));

      this.mat = new THREE.PointsMaterial({
        size: 0.18, map: particleTexture, vertexColors: true, transparent: true,
        opacity: 1.0, blending: THREE.AdditiveBlending, depthWrite: false
      });

      this.points = new THREE.Points(this.geo, this.mat);
      this.group.add(this.points);
      infiniteRootGroup.add(this.group);
      this.currentMorphology = null;
      this.reseed(index * 1.618);
    }

    reseed(seedOffset = Math.random() * 100) {
      this.generationCount++;
      const morphType = MORPHOLOGY_TYPES[Math.floor(Math.random() * MORPHOLOGY_TYPES.length)];
      this.currentMorphology = morphType;
      const seed = seedOffset + this.generationCount * 13.37;
      const themeIndex = Math.floor(Math.random() * 3);

      for (let i = 0; i < PARTICLES_PER_OCTAVE; i++) {
        const [x, y, z] = morphType.func(i, PARTICLES_PER_OCTAVE, seed);
        this.positions[i * 3] = x; this.positions[i * 3 + 1] = y; this.positions[i * 3 + 2] = z;
        this.baseCoordinates[i * 3] = x; this.baseCoordinates[i * 3 + 1] = y; this.baseCoordinates[i * 3 + 2] = z;

        const dist = Math.sqrt(x*x + y*y + z*z);
        let col;
        if (themeIndex === 0) {
          col = dist < 2.0 ? palettePetroleum[0] : (dist < 3.8 ? palettePetroleum[2] : palettePetroleum[4]);
        } else if (themeIndex === 1) {
          col = dist < 2.2 ? palettePetroleum[1] : (dist < 4.0 ? palettePetroleum[3] : palettePetroleum[5]);
        } else {
          col = dist < 1.8 ? palettePetroleum[0] : (dist < 3.5 ? palettePetroleum[4] : palettePetroleum[2]);
        }

        this.colors[i * 3] = Math.min(1.0, col.r * (0.85 + Math.random() * 0.3));
        this.colors[i * 3 + 1] = Math.min(1.0, col.g * (0.85 + Math.random() * 0.3));
        this.colors[i * 3 + 2] = Math.min(1.0, col.b * (0.85 + Math.random() * 0.3));
      }
      this.geo.attributes.position.needsUpdate = true;
      this.geo.attributes.color.needsUpdate = true;
    }
  }

  function initThree(canvas) {
    scene = new THREE.Scene();
    scene.fog = new THREE.FogExp2(0x020408, 0.012);
    camera = new THREE.PerspectiveCamera(55, (window.innerWidth || 1024) / (window.innerHeight || 768), 0.1, 1000);
    camera.position.set(0, 0, 14);

    renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true, powerPreference: 'high-performance' });
    renderer.setSize((window.innerWidth || 1024), (window.innerHeight || 768));
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));

    const particleTexture = createGlowParticleTexture();
    scene.add(new THREE.AmbientLight(0xffffff, 0.8));

    const BG_PARTICLE_COUNT = 7000;
    const bgGeo = new THREE.BufferGeometry();
    const bgPositions = new Float32Array(BG_PARTICLE_COUNT * 3);
    const bgColors = new Float32Array(BG_PARTICLE_COUNT * 3);
    for (let i = 0; i < BG_PARTICLE_COUNT; i++) {
      const radius = 25 + Math.random() * 85;
      const theta = Math.random() * Math.PI * 2;
      const phi = Math.acos(2 * Math.random() - 1);
      bgPositions[i * 3] = radius * Math.sin(phi) * Math.cos(theta);
      bgPositions[i * 3 + 1] = radius * Math.sin(phi) * Math.sin(theta);
      bgPositions[i * 3 + 2] = radius * Math.cos(phi);
      const col = palettePetroleum[Math.floor(Math.random() * palettePetroleum.length)];
      bgColors[i * 3] = col.r; bgColors[i * 3 + 1] = col.g; bgColors[i * 3 + 2] = col.b;
    }
    bgGeo.setAttribute('position', new THREE.BufferAttribute(bgPositions, 3));
    bgGeo.setAttribute('color', new THREE.BufferAttribute(bgColors, 3));
    const bgMat = new THREE.PointsMaterial({ size: 0.28, map: particleTexture, vertexColors: true, transparent: true, opacity: 0.75, blending: THREE.AdditiveBlending, depthWrite: false });
    bgParticleSystem = new THREE.Points(bgGeo, bgMat);
    scene.add(bgParticleSystem);

    infiniteRootGroup = new THREE.Group();
    scene.add(infiniteRootGroup);
    
    for (let i = 0; i < OCTAVE_COUNT; i++) {
      octaves.push(new OctaveLayer(i, particleTexture));
    }

    let isDragging = false, previousPointer = { x: 0, y: 0 };
    const mouse = { x: 0, y: 0, targetX: 0, targetY: 0 };
    const rotationMomentum = { x: 0, y: 0, targetX: 0, targetY: 0 };

    window.addEventListener('wheel', (e) => {
      targetZoom += e.deltaY * 0.0016;
      fluidImpulse = Math.min(1.0, fluidImpulse + Math.abs(e.deltaY * 0.0016) * 0.4);
    }, { passive: false });
    
    window.addEventListener('mousedown', (e) => {
      if (e.target.closest('header, aside, footer, button, input')) return;
      isDragging = true; previousPointer.x = e.clientX; previousPointer.y = e.clientY;
    });
    window.addEventListener('mousemove', (e) => {
      mouse.targetX = (e.clientX / window.innerWidth - 0.5) * 2;
      mouse.targetY = (e.clientY / window.innerHeight - 0.5) * 2;
      if (isDragging) {
        rotationMomentum.targetY += (e.clientX - previousPointer.x) * 0.004;
        rotationMomentum.targetX += (e.clientY - previousPointer.y) * 0.004;
        previousPointer.x = e.clientX; previousPointer.y = e.clientY;
      }
    });
    window.addEventListener('mouseup', () => isDragging = false);
    window.addEventListener('resize', () => {
      camera.aspect = (window.innerWidth || 1024) / (window.innerHeight || 768); camera.updateProjectionMatrix();
      renderer.setSize((window.innerWidth || 1024), (window.innerHeight || 768));
    });

    const clock = new THREE.Clock();
    let lastPassedOctave = 0;

    function animate() {
      requestAnimationFrame(animate);
      const elapsed = clock.getElapsedTime();
      
      if (isAutoCruise) targetZoom += 0.012;
      continuousZoom += (targetZoom - continuousZoom) * 0.12;
      fluidImpulse *= 0.94;

      mouse.x += (mouse.targetX - mouse.x) * 0.05; mouse.y += (mouse.targetY - mouse.y) * 0.05;
      camera.position.x = mouse.x * 0.9; camera.position.y = -mouse.y * 0.7; camera.lookAt(0, 0, 0);

      rotationMomentum.x += (rotationMomentum.targetX - rotationMomentum.x) * 0.08;
      rotationMomentum.y += (rotationMomentum.targetY - rotationMomentum.y) * 0.08;
      infiniteRootGroup.rotation.x = rotationMomentum.x; infiniteRootGroup.rotation.y = rotationMomentum.y + elapsed * 0.035;
      bgParticleSystem.rotation.y = elapsed * 0.015; bgParticleSystem.rotation.x = Math.sin(elapsed * 0.05) * 0.1;

      const currentOctaveFloor = Math.floor(continuousZoom);
      if (currentOctaveFloor !== lastPassedOctave) {
        const wrappedIndex = ((currentOctaveFloor % OCTAVE_COUNT) + OCTAVE_COUNT) % OCTAVE_COUNT;
        octaves[wrappedIndex].reseed();
        lastPassedOctave = currentOctaveFloor;
      }

      let activeDominantOctave = null;
      let maxOctaveOpacity = -1;

      for (let i = 0; i < OCTAVE_COUNT; i++) {
        const oct = octaves[i];
        const shiftedZoom = (continuousZoom + i) % OCTAVE_COUNT;
        const normalizedProgress = ((shiftedZoom % OCTAVE_COUNT) + OCTAVE_COUNT) % OCTAVE_COUNT;
        const scale = Math.pow(SCALE_STEP, (normalizedProgress / OCTAVE_COUNT) * 2.8 - 0.8);
        oct.group.scale.set(scale, scale, scale);

        const progressInOctave = normalizedProgress / OCTAVE_COUNT;
        const dist = Math.abs(progressInOctave - 0.5);
        const opacity = dist >= 0.5 ? 0.0 : Math.pow(0.5 * (1.0 + Math.cos(Math.PI * (dist / 0.5))), 1.4);
        oct.mat.opacity = opacity; oct.group.visible = opacity > 0.008;
        
        if (opacity > maxOctaveOpacity) { maxOctaveOpacity = opacity; activeDominantOctave = oct; }

        const posAttr = oct.geo.attributes.position;
        const posArray = posAttr.array;
        const baseArray = oct.baseCoordinates;
        const waveFreq = elapsed * 1.6 + i;
        const impulseMag = fluidImpulse * 0.45;
        for (let p = 0; p < posArray.length / 3; p += 4) {
          const wave = Math.sin(baseArray[p * 3] * 0.8 + waveFreq) * 0.06 * (1.0 + impulseMag);
          posArray[p * 3] = baseArray[p * 3] * (1.0 + wave);
          posArray[p * 3 + 1] = baseArray[p * 3 + 1] * (1.0 + wave);
          posArray[p * 3 + 2] = baseArray[p * 3 + 2] * (1.0 + wave);
        }
        posAttr.needsUpdate = true;
      }

      if (activeDominantOctave && activeDominantOctave.currentMorphology) {
        const dt = document.getElementById('depth-octave-title');
        const dl = document.getElementById('depth-factor-label');
        const amn = document.getElementById('active-morphology-name');
        const asl = document.getElementById('active-seed-label');
        if(dt) {
           dt.innerHTML = `<span class="w-2 h-2 rounded-full bg-crude-teal animate-pulse"></span><span>OCTAVE ${Math.abs(Math.floor(continuousZoom)) % 10}: ${activeDominantOctave.currentMorphology.name}</span>`;
        }
        if(dl) {
           dl.innerText = `SCALE: ${Math.pow(SCALE_STEP, ((continuousZoom % 1.0) + 1.0) % 1.0).toFixed(3)}x CONTINUOUS // INFINITE LOOP`;
        }
        if(amn) amn.innerText = `SWARM: ${activeDominantOctave.currentMorphology.name}`;
        if(asl) asl.innerText = `GEN #${activeDominantOctave.generationCount} // SEED: 0x${Math.abs(Math.floor(continuousZoom * 71)).toString(16).toUpperCase()}`;
      }
      
      const bars = document.querySelectorAll('.harmonic-bar');
      bars.forEach((b, i) => {
        b.style.height = Math.max(3, 4 + Math.sin(elapsed * 4.5 + i * 0.55) * 14 + fluidImpulse * 14) + "px";
      });
      
      const sparkPath = document.getElementById('sparkline-path');
      if (sparkPath) sparkPath.setAttribute('d', `M 0 20 Q 25 ${(16 + Math.sin(elapsed * 2.8) * 6).toFixed(1)}, 50 16 T 80 ${(10 + Math.cos(elapsed * 2.8 * 1.2) * 5).toFixed(1)} T 100 ${(6 + Math.sin(elapsed * 2.8 * 0.9) * 4).toFixed(1)}`);

      renderer.render(scene, camera);
    }
    animate();
  }
})();
