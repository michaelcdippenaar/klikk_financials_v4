module.exports = {
  extends: [
    'plugin:vue/vue3-essential',
    'eslint:recommended'
  ],
  parserOptions: {
    ecmaVersion: 2020,
    sourceType: 'module'
  },
  env: {
    browser: true,
    node: true,
    es2020: true
  },
  rules: {
    'vue/multi-word-component-names': 'off',
    'no-unused-vars': ['warn', { argsIgnorePattern: '^_' }],
  }
}
