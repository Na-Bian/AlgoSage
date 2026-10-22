/* AlgoSage reusable avatar crop dialog.
   Vanilla Pointer Events + Canvas; the current upload API still performs
   the authoritative file validation and center-crop normalization. */
(() => {
  "use strict";

  const ACCEPTED_TYPES = new Set(["image/png", "image/jpeg", "image/webp"]);
  const MAX_BYTES = 5 * 1024 * 1024;
  const OUTPUT_SIZE = 256;

  function canUseFile(file) {
    if (!file) return "请选择头像文件";
    if (!ACCEPTED_TYPES.has(file.type)) return "头像仅支持 PNG、JPEG 或 WebP";
    if (file.size > MAX_BYTES) return "头像文件不能超过 5MB";
    return "";
  }

  async function decodeImage(file) {
    if (window.createImageBitmap) {
      try {
        return await createImageBitmap(file, { imageOrientation: "from-image" });
      } catch (_) {
        // Fall through to the HTMLImageElement path for older engines.
      }
    }
    return new Promise((resolve, reject) => {
      const url = URL.createObjectURL(file);
      const image = new Image();
      image.onload = () => {
        URL.revokeObjectURL(url);
        resolve(image);
      };
      image.onerror = () => {
        URL.revokeObjectURL(url);
        reject(new Error("图片读取失败，请更换文件后重试"));
      };
      image.src = url;
    });
  }

  function createMarkup() {
    const overlay = document.createElement("div");
    overlay.className = "eg-crop-overlay";
    overlay.hidden = true;
    overlay.innerHTML = `
      <section class="eg-crop-modal" role="dialog" aria-modal="true" aria-labelledby="eg-crop-title" tabindex="-1">
        <header class="eg-crop-header">
          <h2 id="eg-crop-title">调整头像</h2>
        </header>
        <div class="eg-crop-stage" data-crop-stage>
          <canvas class="eg-crop-canvas" data-crop-canvas aria-label="头像裁剪区域"></canvas>
          <div class="eg-crop-mask" aria-hidden="true"></div>
        </div>
        <div class="eg-crop-controls">
          <button class="eg-crop-scale-button" type="button" data-crop-zoom-out aria-label="缩小头像">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M5 12h14"/></svg>
          </button>
          <input class="eg-crop-range" type="range" min="100" max="320" step="1" value="100" aria-label="调整头像缩放比例" data-crop-range>
          <button class="eg-crop-scale-button" type="button" data-crop-zoom-in aria-label="放大头像">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M12 5v14M5 12h14"/></svg>
          </button>
          <output class="eg-crop-scale-value" data-crop-scale-value>100%</output>
        </div>
        <p class="eg-crop-status" role="alert" data-crop-status></p>
        <div class="eg-crop-actions">
          <button class="eg-crop-secondary" type="button" data-crop-cancel>取消</button>
          <button class="eg-crop-primary" type="button" data-crop-save>
            <span data-crop-save-label>保存头像</span>
          </button>
        </div>
      </section>
    `;
    return overlay;
  }

  class AvatarCrop {
    constructor() {
      this.overlay = createMarkup();
      this.modal = this.overlay.querySelector(".eg-crop-modal");
      this.stage = this.overlay.querySelector("[data-crop-stage]");
      this.canvas = this.overlay.querySelector("[data-crop-canvas]");
      this.range = this.overlay.querySelector("[data-crop-range]");
      this.scaleValue = this.overlay.querySelector("[data-crop-scale-value]");
      this.statusNode = this.overlay.querySelector("[data-crop-status]");
      this.saveButton = this.overlay.querySelector("[data-crop-save]");
      this.saveLabel = this.overlay.querySelector("[data-crop-save-label]");
      this.cancelButton = this.overlay.querySelector("[data-crop-cancel]");

      this.source = null;
      this.objectUrl = "";
      this.baseScale = 1;
      this.scale = 1;
      this.offset = { x: 0, y: 0 };
      this.pointers = new Map();
      this.dragAnchor = null;
      this.pinchAnchor = null;
      this.saving = false;
      this.onSave = null;
      this.previousFocus = null;

      this.canvas.addEventListener("pointerdown", this.onPointerDown.bind(this));
      this.canvas.addEventListener("pointermove", this.onPointerMove.bind(this));
      this.canvas.addEventListener("pointerup", this.onPointerUp.bind(this));
      this.canvas.addEventListener("pointercancel", this.onPointerUp.bind(this));
      this.canvas.addEventListener("wheel", this.onWheel.bind(this), { passive: false });
      this.stage.addEventListener("dragstart", (event) => event.preventDefault());
      this.range.addEventListener("input", () => this.setScaleFromRange());
      this.overlay.querySelector("[data-crop-zoom-in]").addEventListener("click", () => this.stepScale(1));
      this.overlay.querySelector("[data-crop-zoom-out]").addEventListener("click", () => this.stepScale(-1));
      this.cancelButton.addEventListener("click", () => this.close());
      this.saveButton.addEventListener("click", () => this.save());
      this.overlay.addEventListener("pointerdown", (event) => {
        if (event.target === this.overlay && !this.saving) this.close();
      });
      document.addEventListener("keydown", this.onKeyDown.bind(this));
      window.addEventListener("resize", () => {
        if (this.overlay.hidden || !this.source) return;
        this.clampOffset();
        this.render();
      });
      document.body.appendChild(this.overlay);
    }

    onKeyDown(event) {
      if (this.overlay.hidden) return;
      if (event.key === "Escape" && !this.saving) {
        event.preventDefault();
        this.close();
        return;
      }
      if (event.key !== "Tab") return;
      const focusable = [...this.modal.querySelectorAll("button,input,[tabindex]:not([tabindex='-1'])")]
        .filter((node) => !node.disabled && node.offsetParent !== null);
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable.at(-1);
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }

    setStatus(message) {
      this.statusNode.textContent = message || "";
      this.statusNode.dataset.kind = message ? "error" : "";
    }

    setSaving(saving) {
      this.saving = saving;
      this.saveButton.disabled = saving;
      this.cancelButton.disabled = saving;
      this.saveButton.setAttribute("aria-busy", String(saving));
      this.saveLabel.textContent = saving ? "保存中…" : "保存头像";
    }

    open(file, options = {}) {
      const validationError = canUseFile(file);
      if (validationError) throw new Error(validationError);
      if (this.saving) return;

      this.onSave = options.onSave || null;
      this.setStatus("");
      this.previousFocus = document.activeElement;
      this.overlay.hidden = false;
      document.body.style.overflow = "hidden";
      this.cancelButton.focus();
      this.source = null;

      decodeImage(file).then((source) => {
        if (this.overlay.hidden) return;
        this.source = source;
        if (source instanceof ImageBitmap) this.objectUrl = "";
        this.initializeGeometry();
      }).catch((error) => {
        this.setStatus(error.message || "图片读取失败");
        if (!this.previousFocus || !(this.previousFocus instanceof HTMLElement)) return;
        this.previousFocus.focus();
      });
    }

    initializeGeometry() {
      requestAnimationFrame(() => {
        if (!this.source || this.overlay.hidden) return;
        const stageSize = this.stage.clientWidth;
        const cropSize = Math.max(120, stageSize - 20);
        const smaller = Math.min(this.source.width, this.source.height);
        this.baseScale = cropSize / smaller;
        this.scale = this.baseScale;
        this.offset = { x: 0, y: 0 };
        this.range.value = "100";
        this.updateScaleValue();
        this.render();
        this.modal.focus({ preventScroll: true });
      });
    }

    get cropGeometry() {
      const stageSize = this.stage.clientWidth;
      const cropSize = Math.max(120, stageSize - 20);
      return {
        stageSize,
        cropSize,
        cropLeft: (stageSize - cropSize) / 2,
        cropTop: (stageSize - cropSize) / 2,
      };
    }

    clampOffset() {
      const { cropSize } = this.cropGeometry;
      const width = this.source.width * this.scale;
      const height = this.source.height * this.scale;
      const maxX = Math.max(0, (width - cropSize) / 2);
      const maxY = Math.max(0, (height - cropSize) / 2);
      this.offset.x = Math.min(maxX, Math.max(-maxX, this.offset.x));
      this.offset.y = Math.min(maxY, Math.max(-maxY, this.offset.y));
    }

    setScale(nextScale) {
      if (!this.source) return;
      const { cropSize } = this.cropGeometry;
      const minimum = this.baseScale;
      const maximum = Math.max(minimum * 1.15, Math.min(minimum * 3.2, (Math.min(this.source.width, this.source.height) * minimum * 8) / cropSize));
      this.scale = Math.min(maximum, Math.max(minimum, nextScale));
      this.clampOffset();
      this.updateScaleValue();
      this.render();
    }

    setScaleFromRange() {
      if (!this.source) return;
      this.setScale(this.baseScale * Number(this.range.value) / 100);
    }

    stepScale(direction) {
      if (!this.source) return;
      const currentPercent = this.scale / this.baseScale * 100;
      this.range.value = String(Math.min(Number(this.range.max), Math.max(Number(this.range.min), Math.round(currentPercent + direction * 10))));
      this.setScaleFromRange();
    }

    updateScaleValue() {
      this.scaleValue.textContent = `${Math.round(this.scale / this.baseScale * 100)}%`;
      if (Number(this.range.value) !== Math.round(this.scale / this.baseScale * 100)) {
        this.range.value = String(Math.round(this.scale / this.baseScale * 100));
      }
    }

    onPointerDown(event) {
      if (!this.source || this.saving) return;
      this.canvas.setPointerCapture(event.pointerId);
      this.pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
      if (this.pointers.size === 1) {
        this.dragAnchor = { x: event.clientX, y: event.clientY, offsetX: this.offset.x, offsetY: this.offset.y };
      } else if (this.pointers.size === 2) {
        const points = [...this.pointers.values()];
        this.pinchAnchor = {
          distance: Math.hypot(points[0].x - points[1].x, points[0].y - points[1].y),
          scale: this.scale,
        };
        this.dragAnchor = null;
      }
    }

    onPointerMove(event) {
      if (!this.source || !this.pointers.has(event.pointerId)) return;
      this.pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });

      if (this.pointers.size === 1 && this.dragAnchor) {
        this.offset.x = this.dragAnchor.offsetX + event.clientX - this.dragAnchor.x;
        this.offset.y = this.dragAnchor.offsetY + event.clientY - this.dragAnchor.y;
        this.clampOffset();
        this.render();
        return;
      }

      if (this.pointers.size === 2 && this.pinchAnchor) {
        const points = [...this.pointers.values()];
        const distance = Math.hypot(points[0].x - points[1].x, points[0].y - points[1].y);
        this.setScale(this.pinchAnchor.scale * distance / (this.pinchAnchor.distance || 1));
      }
    }

    onPointerUp(event) {
      this.pointers.delete(event.pointerId);
      if (this.pointers.size === 1) {
        const [pointer] = this.pointers.values();
        this.dragAnchor = { x: pointer.x, y: pointer.y, offsetX: this.offset.x, offsetY: this.offset.y };
        this.pinchAnchor = null;
      } else if (!this.pointers.size) {
        this.dragAnchor = null;
        this.pinchAnchor = null;
      }
    }

    onWheel(event) {
      if (!this.source || this.saving) return;
      event.preventDefault();
      this.setScale(this.scale * Math.exp(-event.deltaY * 0.0015));
    }

    render() {
      if (!this.source) return;
      const dpr = Math.min(window.devicePixelRatio || 1, 3);
      const stageSize = this.stage.clientWidth;
      const { cropSize, cropLeft, cropTop } = this.cropGeometry;
      this.canvas.width = Math.round(stageSize * dpr);
      this.canvas.height = Math.round(stageSize * dpr);
      this.canvas.style.width = `${stageSize}px`;
      this.canvas.style.height = `${stageSize}px`;
      const context = this.canvas.getContext("2d", { alpha: false });
      context.setTransform(dpr, 0, 0, dpr, 0, 0);
      context.fillStyle = "#0b1120";
      context.fillRect(0, 0, stageSize, stageSize);
      context.imageSmoothingEnabled = true;
      context.imageSmoothingQuality = "high";

      const width = this.source.width * this.scale;
      const height = this.source.height * this.scale;
      const centerX = cropLeft + cropSize / 2 + this.offset.x;
      const centerY = cropTop + cropSize / 2 + this.offset.y;
      context.drawImage(this.source, centerX - width / 2, centerY - height / 2, width, height);
    }

    async toBlob() {
      const { cropSize, cropLeft, cropTop } = this.cropGeometry;
      const sourceX = (cropLeft - (this.cropGeometry.stageSize / 2 - this.source.width * this.scale / 2 + this.offset.x)) / this.scale;
      const sourceY = (cropTop - (this.cropGeometry.stageSize / 2 - this.source.height * this.scale / 2 + this.offset.y)) / this.scale;
      const sourceSize = cropSize / this.scale;
      const canvas = document.createElement("canvas");
      canvas.width = OUTPUT_SIZE;
      canvas.height = OUTPUT_SIZE;
      const context = canvas.getContext("2d");
      context.imageSmoothingEnabled = true;
      context.imageSmoothingQuality = "high";
      context.fillStyle = "#ffffff";
      context.fillRect(0, 0, OUTPUT_SIZE, OUTPUT_SIZE);
      context.drawImage(this.source, sourceX, sourceY, sourceSize, sourceSize, 0, 0, OUTPUT_SIZE, OUTPUT_SIZE);
      const type = this.source.fileType || "image/jpeg";
      const blob = await new Promise((resolve, reject) => {
        canvas.toBlob(result => result ? resolve(result) : reject(new Error("头像裁剪结果生成失败")), type, 0.93);
      });
      const extension = type === "image/png" ? "png" : type === "image/webp" && canvas.toDataURL("image/webp").startsWith("data:image/webp") ? "webp" : "jpg";
      return new File([blob], `avatar-crop.${extension}`, { type: blob.type || type });
    }

    async save() {
      if (this.saving || typeof this.onSave !== "function") return;
      this.setSaving(true);
      this.setStatus("");
      try {
        const file = await this.toBlob();
        await this.onSave(file);
        this.setSaving(false);
        this.close();
      } catch (error) {
        // Keep the temporary crop so the user can correct the failure and retry.
        this.setSaving(false);
        this.setStatus(error.message || "头像保存未完成");
      }
    }

    close() {
      if (this.saving) return;
      this.overlay.hidden = true;
      document.body.style.overflow = "";
      this.pointers.clear();
      this.dragAnchor = null;
      this.pinchAnchor = null;
      if (this.source instanceof ImageBitmap) this.source.close();
      this.source = null;
      this.onSave = null;
      this.setStatus("");
      if (this.previousFocus instanceof HTMLElement) this.previousFocus.focus({ preventScroll: true });
      this.previousFocus = null;
    }
  }

  let dialog;
  function getDialog() {
    if (!dialog) dialog = new AvatarCrop();
    return dialog;
  }

  window.AlgoSageAvatarCrop = {
    validate: canUseFile,
    open(file, options) {
      getDialog().open(file, options);
    },
  };
})();
