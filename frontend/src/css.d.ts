// esbuild resolves `import "./styles.css"` and extracts it into the sibling app.css.
// tsc needs a module shape for the import to typecheck; the module has no runtime value.
declare module "*.css";
