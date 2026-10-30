{{flutter_js}}
{{flutter_build_config}}

// webparagraph-gallery-perf bootstrap: ?web_paragraph=true toggles WebParagraph,
// and the GPU attestation line tells whether WebGL runs on hardware or not at all
const searchParams = new URLSearchParams(window.location.search);
const webParagraph = searchParams.get('web_paragraph') === 'true';
(function attest() {
  const c = document.createElement('canvas');
  const gl = c.getContext('webgl2');
  let renderer = 'none';
  if (gl) {
    const ext = gl.getExtension('WEBGL_debug_renderer_info');
    renderer = ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : 'masked';
  }
  console.log('GALLERY_ATTESTATION ' + JSON.stringify({
    webParagraph: webParagraph,
    webgl2Renderer: renderer,
    crossOriginIsolated: self.crossOriginIsolated,
    textClusters: 'TextCluster' in window,
    devicePixelRatio: window.devicePixelRatio,
    userAgent: navigator.userAgent,
  }));
})();
_flutter.loader.load({
  config: {
    canvasKitBaseUrl: '/canvaskit/',
    ...(webParagraph ? {preferWebParagraph: true} : {}),
  },
});
