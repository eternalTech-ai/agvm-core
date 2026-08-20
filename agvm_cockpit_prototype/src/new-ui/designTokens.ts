export const eternalTechPalette = {
  black: "#0A0F17",
  deepNavy: "#111627",
  darkBlue: "#1C2333",
  mutedBlue: "#2F3C68",
  dustBlue: "#2F3C68",
  cobalt: "#486EFE",
  skyBlue: "#486EFE",
  lavenderBlue: "#D0CCF0",
  pastelIndigo: "#D0CCF0",
  brightMint: "#01EAB2",
  silver: "#B0AFB2",
  paleSilver: "#FFFFFF",
  white: "#FFFFFF",
} as const;

export const eternalTechPaletteNumber = {
  black: 0x0a0f17,
  deepNavy: 0x111627,
  darkBlue: 0x1c2333,
  mutedBlue: 0x2f3c68,
  dustBlue: 0x2f3c68,
  cobalt: 0x486efe,
  skyBlue: 0x486efe,
  lavenderBlue: 0xd0ccf0,
  pastelIndigo: 0xd0ccf0,
  brightMint: 0x01eab2,
  silver: 0xb0afb2,
  paleSilver: 0xffffff,
  white: 0xffffff,
} as const;

export const brainColorRamp = {
  dark: eternalTechPaletteNumber.darkBlue,
  dust: eternalTechPaletteNumber.dustBlue,
  cobalt: eternalTechPaletteNumber.cobalt,
  mint: eternalTechPaletteNumber.brightMint,
  pastel: eternalTechPaletteNumber.pastelIndigo,
  sky: eternalTechPaletteNumber.skyBlue,
  lavender: eternalTechPaletteNumber.lavenderBlue,
  silver: eternalTechPaletteNumber.silver,
} as const;

export const brainRenderPalette = {
  ink: eternalTechPaletteNumber.darkBlue,
  navy: eternalTechPaletteNumber.darkBlue,
  steel: eternalTechPaletteNumber.darkBlue,
  dark: eternalTechPaletteNumber.darkBlue,
  indigo: eternalTechPaletteNumber.dustBlue,
  dust: eternalTechPaletteNumber.dustBlue,
  blue: eternalTechPaletteNumber.cobalt,
  cobalt: eternalTechPaletteNumber.cobalt,
  teal: eternalTechPaletteNumber.brightMint,
  mint: eternalTechPaletteNumber.brightMint,
  sky: eternalTechPaletteNumber.skyBlue,
  lavender: eternalTechPaletteNumber.lavenderBlue,
  pastel: eternalTechPaletteNumber.pastelIndigo,
  silver: eternalTechPaletteNumber.silver,
  mist: eternalTechPaletteNumber.paleSilver,
  hot: eternalTechPaletteNumber.brightMint,
  doc: eternalTechPaletteNumber.brightMint,
  bad: eternalTechPaletteNumber.cobalt,
  white: eternalTechPaletteNumber.white,
} as const;

export const brainLayerLegend = {
  knowledgeNodes: eternalTechPalette.cobalt,
  memoryField: eternalTechPalette.pastelIndigo,
  documentAnchor: eternalTechPalette.brightMint,
  observedLinks: eternalTechPalette.dustBlue,
} as const;
