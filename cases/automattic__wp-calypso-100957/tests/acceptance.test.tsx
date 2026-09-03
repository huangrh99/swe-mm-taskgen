import { render } from '@testing-library/react';
import React from 'react';
import GlobalStylesVariationPreview from '../preview';

const globalStyles: Record<string, unknown> = {};
const globalSettings: Record<string, unknown> = {};

jest.mock( '../../../gutenberg-bridge', () => ( {
	useGlobalStyle: ( path: string ) => [ globalStyles[ path ] ],
	useGlobalSetting: ( path: string ) => [ globalSettings[ path ] ],
	useSafeGlobalStylesOutput: () => [ [] ],
} ) );

jest.mock( '@wordpress/compose', () => ( {
	useReducedMotion: () => true,
	useResizeObserver: () => [ jest.fn(), { width: 248 } ],
} ) );

jest.mock( '@wordpress/element', () => ( {
	useState: jest.requireActual( 'react' ).useState,
} ) );

jest.mock( '@wordpress/components', () => {
	const ReactModule = jest.requireActual( 'react' );
	const Div = ( {
		children,
		spacing: _spacing,
		justify: _justify,
		variants: _variants,
		animate: _animate,
		initial: _initial,
		transition: _transition,
		...props
	}: React.HTMLAttributes< HTMLDivElement > & Record< string, unknown > ) => (
		<div { ...props }>{ children }</div>
	);
	return {
		__unstableMotion: { div: Div },
		__experimentalHStack: Div,
		__experimentalVStack: Div,
		__esModule: true,
		React: ReactModule,
	};
} );

jest.mock( '../../global-styles-variation-container', () => ( {
	__esModule: true,
	default: ( { children }: { children: React.ReactNode } ) => (
		<div data-testid="variation-container">{ children }</div>
	),
} ) );

const palette = ( entries: Array< [ string, string ] > ) =>
	entries.map( ( [ slug, color ] ) => ( { slug, color } ) );

const renderedCircleColors = () =>
	Array.from( document.querySelectorAll( 'div[style*="border-radius"]' ) ).map(
		( node ) => ( node as HTMLDivElement ).style.background
	);

const rgbChannels = ( color: string ) => {
	const match = color.match( /^rgb\((\d+),\s*(\d+),\s*(\d+)\)$/ );
	if ( ! match ) throw new Error( `Expected an opaque rgb() color, received: ${ color }` );
	return match.slice( 1 ).map( Number );
};

const relativeLuminance = ( color: string ) => {
	const channels = rgbChannels( color ).map( ( channel ) => {
		const value = channel / 255;
		return value <= 0.04045 ? value / 12.92 : ( ( value + 0.055 ) / 1.055 ) ** 2.4;
	} );
	return 0.2126 * channels[ 0 ] + 0.7152 * channels[ 1 ] + 0.0722 * channels[ 2 ];
};

const contrastRatio = ( foreground: string, background: string ) => {
	const foregroundLuminance = relativeLuminance( foreground );
	const backgroundLuminance = relativeLuminance( background );
	return (
		( Math.max( foregroundLuminance, backgroundLuminance ) + 0.05 ) /
		( Math.min( foregroundLuminance, backgroundLuminance ) + 0.05 )
	);
};

// Human-calibrated acceptance threshold. This remains below the weakest matching
// After screenshot (4.2554:1) and above both reproduced Before cases (~1.1:1).
const MINIMUM_SWATCH_CONTRAST = 2;

const expectTwoContrastingSwatches = ( background: string ) => {
	const colors = renderedCircleColors();
	expect( colors ).toHaveLength( 2 );
	for ( const color of colors ) {
		expect( contrastRatio( color, background ) ).toBeGreaterThanOrEqual(
			MINIMUM_SWATCH_CONTRAST
		);
	}
};

describe( 'GlobalStylesVariationPreview visual acceptance contract', () => {
	beforeEach( () => {
		for ( const key of Object.keys( globalStyles ) ) delete globalStyles[ key ];
		for ( const key of Object.keys( globalSettings ) ) delete globalSettings[ key ];
	} );

	it( 'separates both swatches from the light card background shown in the PR', () => {
		globalStyles[ 'color.text' ] = '#706e6b';
		globalStyles[ 'color.background' ] = '#efeae6';
		globalStyles[ 'elements.h1.color.text' ] = '#383532';
		globalStyles[ 'elements.button.color.background' ] = '#383532';
		globalSettings[ 'color.palette.theme' ] = palette( [
			[ 'background', '#efeae6' ],
			[ 'heading', '#383532' ],
			[ 'before-low-contrast', '#f3f0ed' ],
			[ 'text', '#706e6b' ],
			[ 'button', '#383532' ],
		] );

		render( <GlobalStylesVariationPreview title="Variation" /> );

		expectTwoContrastingSwatches( 'rgb(239, 234, 230)' );
	} );

	it( 'separates both swatches from the lilac card background shown in the PR', () => {
		globalStyles[ 'color.text' ] = '#4c4653';
		globalStyles[ 'color.background' ] = '#d6d0db';
		globalStyles[ 'elements.h1.color.text' ] = '#4c4653';
		globalStyles[ 'elements.button.color.background' ] = '#6e20d1';
		globalSettings[ 'color.palette.theme' ] = palette( [
			[ 'background', '#d6d0db' ],
			[ 'heading', '#4c4653' ],
			[ 'before-low-contrast', '#e5daf4' ],
			[ 'button', '#6e20d1' ],
		] );

		render( <GlobalStylesVariationPreview title="Variation" /> );

		expectTwoContrastingSwatches( 'rgb(214, 208, 219)' );
	} );

	it( 'preserves the configured preview background color', () => {
		globalStyles[ 'color.background' ] = '#f0e0d0';

		const { getByTestId } = render( <GlobalStylesVariationPreview title="Variation" /> );

		const preview = getByTestId( 'variation-container' ).firstElementChild as HTMLDivElement;
		expect( preview.style.background ).toBe( 'rgb(240, 224, 208)' );
	} );

	it( 'preserves the configured heading color used by the title frame', () => {
		globalStyles[ 'color.text' ] = '#111111';
		globalStyles[ 'elements.h1.color.text' ] = '#663399';

		const { getByText } = render( <GlobalStylesVariationPreview title="Variation" /> );

		expect( getByText( 'Variation' ) ).toHaveStyle( { color: '#663399' } );
	} );
} );
