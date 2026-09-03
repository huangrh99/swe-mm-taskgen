import { render } from '@testing-library/react';
import React from 'react';
import GlobalStylesVariationPreview from '../../packages/global-styles/src/components/global-styles-variations/preview';

const mockGlobalStyles: Record<string, unknown> = {};
const mockGlobalSettings: Record<string, unknown> = {};

jest.mock('../../packages/global-styles/src/gutenberg-bridge', () => ({
	useGlobalStyle: (path: string) => [mockGlobalStyles[path]],
	useGlobalSetting: (path: string) => [mockGlobalSettings[path]],
	useSafeGlobalStylesOutput: () => [[]],
}));

jest.mock('../../packages/global-styles/src/components/global-styles-variation-container', () => {
	return function MockContainer({ children }: { children: React.ReactNode }) {
		return <div data-testid="variation-container">{children}</div>;
	};
});

jest.mock('framer-motion', () => ({
	motion: {
		div: ({ style, ...props }: React.HTMLAttributes<HTMLDivElement>) => (
			<div data-testid="color-swatch" style={style} {...props} />
		),
	},
}));

describe('GlobalStylesVariationPreview', () => {
	beforeEach(() => {
		Object.keys(mockGlobalStyles).forEach((key) => delete mockGlobalStyles[key]);
		Object.keys(mockGlobalSettings).forEach((key) => delete mockGlobalSettings[key]);
	});

	it('should prioritize text and button background colors in highlighted swatches', () => {
		mockGlobalStyles['color.text'] = '#111111';
		mockGlobalStyles['color.background'] = '#ffffff';
		mockGlobalStyles['elements.button.color.background'] = '#0055ff';
		mockGlobalSettings['color.palette.theme'] = [
			{ slug: 'random-1', color: '#ff0000' },
			{ slug: 'random-2', color: '#00ff00' },
			{ slug: 'text', color: '#111111' },
			{ slug: 'button', color: '#0055ff' },
		];

		const { getAllByTestId } = render(
			<GlobalStylesVariationPreview
				title="Test Variation"
				inlineCss=""
				isFocused={false}
				onFocusOut={jest.fn()}
			/>
		);

		const swatches = getAllByTestId('color-swatch');
		expect(swatches).toHaveLength(2);
		expect(swatches[0].style.backgroundColor).toBe('rgb(17, 17, 17)');
		expect(swatches[1].style.backgroundColor).toBe('rgb(0, 85, 255)');
	});

	it('should fallback button background to link color when button background is not explicitly defined', () => {
		mockGlobalStyles['color.text'] = '#222222';
		mockGlobalStyles['color.background'] = '#ffffff';
		mockGlobalStyles['elements.link.color.text'] = '#00aa00';
		mockGlobalSettings['color.palette.theme'] = [
			{ slug: 'palette-1', color: '#ee0000' },
			{ slug: 'palette-link', color: '#00aa00' },
			{ slug: 'palette-text', color: '#222222' },
		];

		const { getAllByTestId } = render(
			<GlobalStylesVariationPreview
				title="Link Fallback Variation"
				inlineCss=""
				isFocused={false}
				onFocusOut={jest.fn()}
			/>
		);

		const swatches = getAllByTestId('color-swatch');
		expect(swatches).toHaveLength(2);
		expect(swatches[0].style.backgroundColor).toBe('rgb(34, 34, 34)');
		expect(swatches[1].style.backgroundColor).toBe('rgb(0, 170, 0)');
	});

	it('should render swatches without key collisions when duplicate slugs exist', () => {
		const consoleErrorSpy = jest.spyOn(console, 'error').mockImplementation(() => {});
		mockGlobalStyles['color.text'] = '#111111';
		mockGlobalStyles['color.background'] = '#ffffff';
		mockGlobalSettings['color.palette.theme'] = [
			{ slug: 'duplicate-slug', color: '#111111' },
			{ slug: 'duplicate-slug', color: '#222222' },
		];

		const { getAllByTestId } = render(
			<GlobalStylesVariationPreview
				title="Duplicate Slug Variation"
				inlineCss=""
				isFocused={false}
				onFocusOut={jest.fn()}
			/>
		);

		const swatches = getAllByTestId('color-swatch');
		expect(swatches).toHaveLength(2);
		expect(consoleErrorSpy).not.toHaveBeenCalledWith(
			expect.stringContaining('Encountered two children with the same key')
		);
		consoleErrorSpy.mockRestore();
	});
});
